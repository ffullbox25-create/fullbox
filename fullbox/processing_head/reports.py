from __future__ import annotations

import io
from datetime import datetime, time, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView
from openpyxl import Workbook

from audit.models import get_order_external_number
from employees.access import RoleRequiredMixin
from employees.models import Employee
from marking.models import MarkingCode
from processing_app.closed_discrepancy_audit import (
    audit_closed_processing_discrepancies,
)
from processing_app.closed_discrepancy_review import (
    CORRECTION_TASK_TITLE_PREFIX,
    REVIEW_STATUS_CHOICES,
    REVIEW_STATUS_LABELS,
    REVIEW_STATUS_CORRECTION_REQUIRED,
    REVIEW_STATUS_EXPLAINED,
    REVIEW_STATUS_UNDER_REVIEW,
    ClosedDiscrepancyReviewError,
    closed_discrepancy_correction_task_route,
    create_or_update_closed_discrepancy_correction_task,
    review_closed_processing_discrepancy,
)
from sklad.models import WarehouseEvent, WarehouseStockSnapshot
from todo.models import Task

from .employee_report import processing_employee_fact_rows
from .views import PROCESSING_ORDER_Q, PROCESSING_WORKFLOW_ROLE_Q, _build_task_rows, _short_name


REPORTS = {
    "kiz": {"title": "Отчёт по КИЗам", "description": "Поступление, использование, резервирование, списание и доступный остаток кодов маркировки.", "icon": "⌁"},
    "product-movement": {"title": "Отчёт по движению товаров", "description": "Операции поступления, обработки, отгрузки, возврата и списания товара.", "icon": "⇄"},
    "processing": {"title": "Отчёт по обработке заявок", "description": "Количество, статусы, сроки и результат обработки заявок.", "icon": "▤"},
    "inventory": {"title": "Отчёт по остаткам", "description": "Актуальные остатки товара и резервы участка обработки.", "icon": "□"},
    "employees": {
        "title": "Отчёт по сотрудникам",
        "description": "Фактически выполненная обработка и формирование коробов по сотрудникам, клиентам, заявкам и товарам.",
        "icon": "◌",
    },
    "deviations": {"title": "Отчёт по ошибкам и отклонениям", "description": "Просрочки, расхождения, проблемы и заявки без исполнителя.", "icon": "!"},
    "closed-discrepancies": {
        "title": "Закрытые заявки с расхождениями",
        "description": "Контролируемый разбор старых закрытых заявок без автоматического изменения количеств и складских данных.",
        "icon": "≠",
    },
}
PERIODS = (
    ("today", "Сегодня"), ("yesterday", "Вчера"), ("week", "Текущая неделя"),
    ("month", "Текущий месяц"), ("previous_month", "Прошлый месяц"),
    ("all", "За всё время"), ("custom", "Произвольный период"),
)


def _number(value):
    return f"{int(value or 0):,}".replace(",", " ")


def _period(request, default_key="month"):
    key = str(request.GET.get("period") or default_key).strip()
    if key not in dict(PERIODS):
        key = "month"
    today = timezone.localdate()
    if key == "today":
        start, end = today, today
    elif key == "yesterday":
        start = end = today - timedelta(days=1)
    elif key == "week":
        start, end = today - timedelta(days=today.weekday()), today
    elif key == "previous_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif key == "all":
        start, end = today.replace(year=2000, month=1, day=1), today
    elif key == "custom":
        try:
            start = datetime.strptime(request.GET.get("date_from", ""), "%Y-%m-%d").date()
            end = datetime.strptime(request.GET.get("date_to", ""), "%Y-%m-%d").date()
        except ValueError:
            start = today.replace(day=1)
            end = today
    else:
        start, end = today.replace(day=1), today
    if end < start:
        start, end = end, start
    tz = timezone.get_current_timezone()
    return key, start, end, timezone.make_aware(datetime.combine(start, time.min), tz), timezone.make_aware(datetime.combine(end, time.max), tz)


def _filters(request):
    return {key: str(request.GET.get(key) or "").strip() for key in ("client", "q", "executor", "status", "operation", "per_page", "sort", "direction", "date_from", "date_to")}


def _keep_query(request, **overrides):
    query = request.GET.copy()
    for key, value in overrides.items():
        if value in (None, ""):
            query.pop(key, None)
        else:
            query[key] = value
    encoded = query.urlencode()
    return f"?{encoded}" if encoded else ""


def _filter_rows(rows, filters):
    query = filters["q"].casefold()
    client = filters["client"].casefold()
    executor = filters["executor"].casefold()
    status = filters["status"].casefold()
    operation = filters["operation"].casefold()
    result = []
    for row in rows:
        if query and query not in " ".join(str(value) for value in row["search"]).casefold():
            continue
        if client and client not in str(row.get("client") or "").casefold():
            continue
        if executor and executor not in str(row.get("executor") or "").casefold():
            continue
        if status and status not in str(row.get("status") or "").casefold():
            continue
        if operation and operation not in str(row.get("operation") or "").casefold():
            continue
        result.append(row)
    return result


def _cell(row, key):
    value = row.get(key, "")
    if key == "date":
        value = timezone.localtime(value).strftime("%d.%m.%Y %H:%M") if value else "—"
    review = row.get("review") if key == "review" else None
    if review:
        value = str(review.get("status_label") or REVIEW_STATUS_LABELS[""])
        if review.get("comment"):
            value = f"{value}: {review['comment']}"
    return {
        "value": value,
        "badge": key in {"status", "review_status"},
        "url": row.get("url") if key == "document" else "",
        "review": review,
    }


def _kiz_rows(start, end):
    rows = []
    for code in MarkingCode.objects.filter(order_type="processing", created_at__range=(start, end)).select_related("agency", "created_by", "used_by")[:1000]:
        used = bool(code.used_at)
        rows.append({
            "date": code.used_at or code.created_at, "client": getattr(code.agency, "short_name", "") or getattr(code.agency, "agn_name", "") or "—",
            "product": code.sku_code or "—", "article": code.sku_code or "—", "code": code.code, "operation": "Использование" if used else "Поступление",
            "qty": 1, "status": "Использован" if used else "Доступен", "document": code.order_id or "—",
            "executor": _short_name(getattr(code.used_by or code.created_by, "get_full_name", lambda: "")() or getattr(code.used_by or code.created_by, "username", "")),
            "url": f"/orders/processing/{code.order_id}/work/" if code.order_id else "", "search": [code.code, code.sku_code, code.order_id],
        })
    return rows


def _movement_rows(start, end):
    queryset = WarehouseEvent.objects.filter(occurred_at__range=(start, end)).filter(
        Q(stock_context_type="processing") | Q(source_document_type="processing")
    ).select_related("agency", "performed_by", "container")[:1000]
    rows = []
    for event in queryset:
        payload = event.payload if isinstance(event.payload, dict) else {}
        article = str(payload.get("sku_code") or payload.get("article") or "—")
        document = event.stock_context_id or event.source_document_id or "—"
        rows.append({
            "date": event.occurred_at, "client": event.agency.short_name or event.agency.agn_name or "—", "operation": event.event_type.replace("_", " ").capitalize(),
            "product": str(payload.get("name") or "—"), "article": article, "qty": event.qty, "status": "Выполнено", "document": document,
            "executor": _short_name(event.performed_by.get_full_name() or event.performed_by.username) if event.performed_by else "Не назначен",
            "url": f"/orders/processing/{document}/work/" if document != "—" else "", "search": [article, document, event.event_type, payload.get("name")],
        })
    return rows


def _processing_rows(start, end):
    tasks = Task.objects.select_related("assigned_to", "observer", "created_by").filter(PROCESSING_ORDER_Q, PROCESSING_WORKFLOW_ROLE_Q).distinct().order_by("-updated_at")[:1000]
    now = timezone.now()
    rows = []
    for item in _build_task_rows(list(tasks), now=now):
        if not start <= item["updated_at_value"] <= end:
            continue
        rows.append({
            "date": item["updated_at_value"], "client": item["client"], "operation": "Обработка заявки", "product": item["title"], "article": "—",
            "qty": 1, "status": item["status_label"], "document": item["title"], "executor": item["executor"], "url": item["url"],
            "overdue": item["queue_bucket"] == "overdue", "unassigned": item["is_unassigned"], "search": [item["title"], item["client"], item["executor"], item["status_label"]],
        })
    return rows


def _inventory_rows():
    snapshots = WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0).filter(
        Q(source_context_type="processing") | Q(zone_code__iexact="OBR") | Q(processing_reserved_qty__gt=0)
    ).select_related("agency")[:1000]
    return [{
        "date": item.updated_at, "client": item.agency.short_name or item.agency.agn_name or "—", "operation": "Остаток", "product": item.name or "—",
        "article": item.sku_code or "—", "qty": item.qty, "available": item.available_qty, "reserved": item.processing_reserved_qty,
        "status": "В норме" if item.available_qty else "Нет в наличии", "document": item.source_context_id or "—", "executor": "—", "url": "",
        "search": [item.sku_code, item.name, item.barcode, item.source_context_id],
    } for item in snapshots]


def _employee_rows(start, end):
    return processing_employee_fact_rows(start, end)


def _employee_summary(rows):
    return [
        (
            "Сотрудников с результатом",
            len(
                {
                    row["executor"]
                    for row in rows
                    if row["executor"] != "Не указан"
                }
            ),
        ),
        ("Клиентов", len({row["client"] for row in rows if row["client"] != "—"})),
        ("Заявок", len({row["document"] for row in rows})),
        ("Обработано единиц", sum(row["processed"] for row in rows)),
        ("Сформировано в короба", sum(row["boxed"] for row in rows)),
        ("Сформировано коробов", sum(row["boxes"] for row in rows)),
        ("Брак", sum(row["defect"] for row in rows)),
        ("Напечатано этикеток", sum(row["labels"] for row in rows)),
    ]


def _employee_totals(rows):
    grouped = {}
    for row in rows:
        key = (row["executor"], row["employee_role"])
        item = grouped.setdefault(
            key,
            {
                "executor": row["executor"],
                "employee_role": row["employee_role"],
                "clients": set(),
                "documents": set(),
                "products": set(),
                "operations": set(),
                "processed": 0,
                "boxed": 0,
                "boxes": 0,
                "defect": 0,
                "labels": 0,
            },
        )
        if row["client"] != "—":
            item["clients"].add(row["client"])
        item["documents"].add(row["document"])
        item["products"].add((row["product"], row["article"]))
        item["operations"].add(row["operation"])
        for key_name in ("processed", "boxed", "boxes", "defect", "labels"):
            item[key_name] += row[key_name]
    totals = []
    for item in grouped.values():
        totals.append(
            {
                "executor": item["executor"],
                "employee_role": item["employee_role"],
                "clients": len(item["clients"]),
                "documents": len(item["documents"]),
                "products": len(item["products"]),
                "operations": ", ".join(sorted(item["operations"])),
                "processed": item["processed"],
                "boxed": item["boxed"],
                "boxes": item["boxes"],
                "defect": item["defect"],
                "labels": item["labels"],
            }
        )
    return sorted(
        totals,
        key=lambda item: (
            -(item["processed"] + item["boxed"]),
            item["executor"].casefold(),
        ),
    )


def _deviation_rows(start, end):
    return [row for row in _processing_rows(start, end) if row.get("overdue") or row.get("unassigned") or "проблем" in row["status"].casefold() or "заблок" in row["status"].casefold()]


def _closed_discrepancy_rows(start, end):
    report = audit_closed_processing_discrepancies(sample_limit=10000)
    correction_task_routes = {
        closed_discrepancy_correction_task_route(item.order_id)
        for item in report.rows
    }
    correction_tasks = {}
    if correction_task_routes:
        task_queryset = (
            Task.objects.filter(
                route__in=correction_task_routes,
                title__startswith=CORRECTION_TASK_TITLE_PREFIX,
            )
            .select_related("assigned_to")
            .order_by("route", "-updated_at", "-id")
        )
        for task in task_queryset:
            correction_tasks.setdefault(task.route, task)
    correction_assignees = list(
        Employee.objects.filter(
            role__in=("processing_head", "processing_worker"),
            is_active=True,
        ).order_by("full_name", "id")
    )
    rows = []
    for item in report.rows:
        completed_at = parse_datetime(item.completed_at)
        if completed_at and timezone.is_naive(completed_at):
            completed_at = timezone.make_aware(
                completed_at,
                timezone.get_current_timezone(),
            )
        if completed_at and not start <= completed_at <= end:
            continue
        review_status = item.review_status or ""
        review_label = REVIEW_STATUS_LABELS.get(
            review_status,
            review_status or REVIEW_STATUS_LABELS[""],
        )
        differences = []
        if "declared_vs_processed" in item.differences:
            differences.append("заявлено ≠ обработано")
        if "processed_vs_boxed" in item.differences:
            differences.append("обработано ≠ в коробах")
        if "declared_vs_boxed" in item.differences:
            differences.append("заявлено ≠ в коробах")
        document = get_order_external_number("processing", item.order_id)
        reviewed_at = parse_datetime(item.reviewed_at)
        if reviewed_at and timezone.is_naive(reviewed_at):
            reviewed_at = timezone.make_aware(
                reviewed_at,
                timezone.get_current_timezone(),
            )
        correction_task = correction_tasks.get(
            closed_discrepancy_correction_task_route(item.order_id)
        )
        correction_due_at = ""
        if correction_task and correction_task.due_date:
            correction_due_at = timezone.localtime(
                correction_task.due_date
            ).strftime("%Y-%m-%dT%H:%M")
        rows.append(
            {
                "date": completed_at,
                "client": item.agency_name or "—",
                "operation": "Закрыто с расхождением",
                "product": " / ".join(differences),
                "article": "—",
                "qty": item.declared_qty,
                "declared": item.declared_qty,
                "processed": item.processed_qty,
                "boxed": item.boxed_qty,
                "differences": "; ".join(differences),
                "status": review_label,
                "review_status": review_label,
                "document": document,
                "executor": item.reviewed_by or "Не назначен",
                "url": f"/orders/processing/{item.order_id}/work/",
                "review": {
                    "url": reverse(
                        "processing-head-closed-discrepancy-review",
                        kwargs={"order_id": item.order_id},
                    ),
                    "status": review_status,
                    "status_label": review_label,
                    "comment": item.review_comment,
                    "choices": REVIEW_STATUS_CHOICES,
                    "reviewed_by": item.reviewed_by,
                    "reviewed_at": (
                        timezone.localtime(reviewed_at).strftime(
                            "%d.%m.%Y %H:%M"
                        )
                        if reviewed_at
                        else ""
                    ),
                    "correction_allowed": (
                        review_status == REVIEW_STATUS_CORRECTION_REQUIRED
                    ),
                    "correction_url": reverse(
                        "processing-head-closed-discrepancy-correction-task",
                        kwargs={"order_id": item.order_id},
                    ),
                    "correction_assignees": correction_assignees,
                    "correction_task_id": (
                        correction_task.pk if correction_task else ""
                    ),
                    "correction_task_status": (
                        correction_task.get_status_display()
                        if correction_task
                        else ""
                    ),
                    "correction_assignee_id": (
                        correction_task.assigned_to_id
                        if correction_task
                        else ""
                    ),
                    "correction_assignee_name": (
                        correction_task.assigned_to.full_name
                        if correction_task and correction_task.assigned_to
                        else ""
                    ),
                    "correction_due_at": correction_due_at,
                    "correction_comment": (
                        correction_task.description.rsplit(
                            "Что исправить: ",
                            1,
                        )[-1]
                        if correction_task
                        else ""
                    ),
                },
                "search": [
                    document,
                    item.order_id,
                    item.agency_name,
                    review_label,
                    item.review_comment,
                    *differences,
                ],
            }
        )
    return rows


def _report_data(slug, start, end):
    if slug == "kiz":
        rows = _kiz_rows(start, end)
        summary = [("Пришло КИЗов", len(rows)), ("Использовано", sum(row["operation"] == "Использование" for row in rows)), ("Зарезервировано", 0), ("Списано / аннулировано", 0), ("Доступно", sum(row["status"] == "Доступен" for row in rows)), ("Процент использования", f"{round(100 * sum(row['operation'] == 'Использование' for row in rows) / len(rows)) if rows else 0}%")]
        columns = [("date", "Дата и время"), ("client", "Клиент"), ("article", "Артикул"), ("operation", "Тип операции"), ("qty", "Количество"), ("status", "Статус"), ("document", "Заявка")]
    elif slug == "product-movement":
        rows = _movement_rows(start, end)
        summary = [("Остаток на начало", 0), ("Поступило", sum(row["qty"] for row in rows)), ("Передано в обработку", sum(row["qty"] for row in rows if "processing" in row["operation"].casefold())), ("Обработано", sum(row["qty"] for row in rows if "обработ" in row["operation"].casefold())), ("Списано", sum(row["qty"] for row in rows if "спис" in row["operation"].casefold())), ("Остаток на конец", 0)]
        columns = [("date", "Дата и время"), ("operation", "Тип движения"), ("client", "Клиент"), ("product", "Товар"), ("article", "Артикул"), ("qty", "Количество"), ("document", "Документ"), ("executor", "Исполнитель")]
    elif slug == "processing":
        rows = _processing_rows(start, end)
        summary = [("Всего заявок", len(rows)), ("В работе", sum(row["status"] not in ("Готово", "Завершено") for row in rows)), ("Завершены", sum(row["status"] in ("Готово", "Завершено") for row in rows)), ("Просрочены", sum(row.get("overdue") for row in rows)), ("Без исполнителя", sum(row.get("unassigned") for row in rows))]
        columns = [("document", "Заявка"), ("client", "Клиент"), ("date", "Дата изменения"), ("status", "Статус"), ("executor", "Исполнитель")]
    elif slug == "inventory":
        rows = _inventory_rows()
        summary = [("Всего единиц", sum(row["qty"] for row in rows)), ("Доступно", sum(row["available"] for row in rows)), ("В обработке", sum(row["qty"] for row in rows)), ("Зарезервировано", sum(row["reserved"] for row in rows)), ("Нет в наличии", sum(row["status"] == "Нет в наличии" for row in rows))]
        columns = [("client", "Клиент"), ("product", "Товар"), ("article", "Артикул"), ("qty", "Остаток"), ("available", "Доступно"), ("reserved", "Резерв"), ("status", "Статус")]
    elif slug == "employees":
        rows = _employee_rows(start, end)
        summary = _employee_summary(rows)
        columns = [
            ("date", "Дата и время"),
            ("executor", "Сотрудник"),
            ("employee_role", "Роль"),
            ("client", "Клиент"),
            ("document", "Заявка"),
            ("operation", "Операция"),
            ("product", "Товар"),
            ("source_article", "Исходный артикул"),
            ("article", "Итоговый артикул"),
            ("size", "Размер"),
            ("processed", "Обработано"),
            ("boxed", "В коробах"),
            ("boxes", "Коробов"),
            ("defect", "Брак"),
            ("shortage", "Недостача"),
            ("labels", "Этикеток"),
            ("tags", "Заменено бирок"),
            ("details", "Параметры / детали"),
            ("status", "Статус"),
        ]
    elif slug == "deviations":
        rows = _deviation_rows(start, end)
        summary = [("Всего отклонений", len(rows)), ("Просрочено", sum(row.get("overdue") for row in rows)), ("Без исполнителя", sum(row.get("unassigned") for row in rows)), ("Критические", sum(row.get("overdue") for row in rows)), ("Устранено", 0)]
        columns = [("date", "Дата и время"), ("status", "Категория"), ("document", "Заявка"), ("client", "Клиент"), ("executor", "Исполнитель")]
    else:
        rows = _closed_discrepancy_rows(start, end)
        summary = [
            ("Всего расхождений", len(rows)),
            (
                "Не разобрано",
                sum(row["review_status"] == REVIEW_STATUS_LABELS[""] for row in rows),
            ),
            (
                "Рассматривается",
                sum(
                    row["review_status"]
                    == REVIEW_STATUS_LABELS[REVIEW_STATUS_UNDER_REVIEW]
                    for row in rows
                ),
            ),
            (
                "Объяснено",
                sum(
                    row["review_status"]
                    == REVIEW_STATUS_LABELS[REVIEW_STATUS_EXPLAINED]
                    for row in rows
                ),
            ),
            (
                "Нужна корректировка",
                sum(
                    row["review_status"]
                    == REVIEW_STATUS_LABELS[REVIEW_STATUS_CORRECTION_REQUIRED]
                    for row in rows
                ),
            ),
        ]
        columns = [
            ("document", "Заявка"),
            ("client", "Клиент"),
            ("date", "Закрыта"),
            ("declared", "Заявлено"),
            ("processed", "Обработано"),
            ("boxed", "В коробах"),
            ("differences", "Расхождение"),
            ("review_status", "Разбор"),
            ("review", "Решение руководителя"),
        ]
    return rows, summary, columns


class ProcessingHeadReportsView(RoleRequiredMixin, TemplateView):
    template_name = "processing_head/reports.html"
    allowed_roles = ("processing_head",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slug = kwargs.get("slug")
        if slug and slug not in REPORTS:
            raise Http404("Отчёт не найден")
        context.update({"reports": REPORTS, "active_report": slug, "report": REPORTS.get(slug), "periods": PERIODS, "filters": _filters(self.request), "catalog_url": reverse("processing-head-reports")})
        if not slug:
            return context
        default_period = "all" if slug == "closed-discrepancies" else "month"
        period_key, date_from, date_to, start, end = _period(
            self.request,
            default_period,
        )
        filters = context["filters"]
        rows, summary, columns = _report_data(slug, start, end)
        rows = _filter_rows(rows, filters)
        if slug == "employees":
            summary = _employee_summary(rows)
            employee_totals = _employee_totals(rows)
        else:
            employee_totals = []
        sort_key = filters["sort"] if filters["sort"] in {key for key, _label in columns} else "date"
        reverse_order = filters["direction"] == "asc"
        rows.sort(key=lambda row: str(row.get(sort_key) or ""), reverse=not reverse_order)
        per_page = int(filters["per_page"]) if filters["per_page"] in {"25", "50", "100"} else 25
        page_obj = Paginator(rows, per_page).get_page(self.request.GET.get("page") or 1)
        context.update({
            "period_key": period_key,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "summary": [(label, _number(value) if isinstance(value, int) else value) for label, value in summary],
            "columns": columns,
            "rows": [{"cells": [_cell(row, key) for key, _label in columns]} for row in page_obj.object_list],
            "page_obj": page_obj,
            "rows_total": len(rows),
            "employee_totals": employee_totals,
            "export_url": f"{self.request.path}{_keep_query(self.request, export='xlsx', page=None)}",
            "refresh_url": f"{self.request.path}{_keep_query(self.request, page=None)}",
            "previous_url": f"{self.request.path}{_keep_query(self.request, page=page_obj.previous_page_number)}" if page_obj.has_previous() else "",
            "next_url": f"{self.request.path}{_keep_query(self.request, page=page_obj.next_page_number)}" if page_obj.has_next() else "",
        })
        return context

    def get(self, request, *args, **kwargs):
        if str(request.GET.get("export") or "") == "xlsx" and kwargs.get("slug"):
            context = self.get_context_data(**kwargs)
            return self._excel(context)
        return super().get(request, *args, **kwargs)

    def _excel(self, context):
        report = context["report"]
        book = Workbook()
        sheet = book.active
        sheet.title = "Отчёт"
        sheet.append([report["title"]])
        sheet.append(["Период", f"{context['date_from']} — {context['date_to']}"])
        sheet.append(["Сформирован", timezone.localtime().strftime("%d.%m.%Y %H:%M")])
        sheet.append([])
        for label, value in context["summary"]:
            sheet.append([label, value])
        sheet.append([])
        sheet.append([label for _key, label in context["columns"]])
        default_period = (
            "all"
            if context["active_report"] == "closed-discrepancies"
            else "month"
        )
        source_rows = _filter_rows(
            _report_data(
                context["active_report"],
                *_period(self.request, default_period)[3:],
            )[0],
            context["filters"],
        )
        for row in source_rows:
            sheet.append([_cell(row, key)["value"] for key, _label in context["columns"]])
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = min(42, max(12, max(len(str(cell.value or "")) for cell in column) + 2))
        if context["active_report"] == "employees":
            totals_sheet = book.create_sheet("По сотрудникам")
            totals_columns = (
                ("executor", "Сотрудник"),
                ("employee_role", "Роль"),
                ("clients", "Клиентов"),
                ("documents", "Заявок"),
                ("products", "Товаров"),
                ("operations", "Операции"),
                ("processed", "Обработано"),
                ("boxed", "В коробах"),
                ("boxes", "Коробов"),
                ("defect", "Брак"),
                ("labels", "Этикеток"),
            )
            totals_sheet.append([label for _key, label in totals_columns])
            for row in context["employee_totals"]:
                totals_sheet.append([row[key] for key, _label in totals_columns])
            for column in totals_sheet.columns:
                totals_sheet.column_dimensions[column[0].column_letter].width = min(
                    42,
                    max(
                        12,
                        max(len(str(cell.value or "")) for cell in column) + 2,
                    ),
                )
        stream = io.BytesIO()
        book.save(stream)
        response = HttpResponse(stream.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        safe_title = report["title"].replace(" ", "_").replace("ё", "е")
        response["Content-Disposition"] = f'attachment; filename="{safe_title}_{context["date_from"]}_{context["date_to"]}.xlsx"'
        return response


class ProcessingClosedDiscrepancyReviewView(RoleRequiredMixin, View):
    allowed_roles = ("processing_head",)
    http_method_names = ("post",)

    def post(self, request, order_id, *args, **kwargs):
        return_url = str(request.POST.get("next") or "").strip()
        if not url_has_allowed_host_and_scheme(
            return_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return_url = reverse(
                "processing-head-report-detail",
                kwargs={"slug": "closed-discrepancies"},
            )
        try:
            result = review_closed_processing_discrepancy(
                order_id=order_id,
                status=request.POST.get("status"),
                comment=request.POST.get("comment"),
                user=request.user,
            )
        except ClosedDiscrepancyReviewError as error:
            messages.error(request, str(error))
        else:
            verb = "зафиксировано" if result.created else "уже было зафиксировано"
            messages.success(
                request,
                f"Решение по заявке {order_id} {verb}: {result.status_label}.",
            )
        return redirect(return_url)


class ProcessingClosedDiscrepancyCorrectionTaskView(
    RoleRequiredMixin,
    View,
):
    allowed_roles = ("processing_head",)
    http_method_names = ("post",)

    def post(self, request, order_id, *args, **kwargs):
        return_url = str(request.POST.get("next") or "").strip()
        if not url_has_allowed_host_and_scheme(
            return_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return_url = reverse(
                "processing-head-report-detail",
                kwargs={"slug": "closed-discrepancies"},
            )
        try:
            result = create_or_update_closed_discrepancy_correction_task(
                order_id=order_id,
                assignee_id=request.POST.get("assignee_id"),
                due_at=parse_datetime(
                    str(request.POST.get("due_at") or "").strip()
                ),
                comment=request.POST.get("comment"),
                user=request.user,
            )
        except ClosedDiscrepancyReviewError as error:
            messages.error(request, str(error))
        else:
            if result.created:
                verb = "создана"
            elif result.updated:
                verb = "обновлена"
            else:
                verb = "уже была создана без изменений"
            messages.success(
                request,
                (
                    f"Задача №{result.task_id} {verb}. "
                    f"Исполнитель: {result.assignee_name}."
                ),
            )
        return redirect(return_url)
