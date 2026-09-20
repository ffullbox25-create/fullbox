"""Полезные read-only отчёты для ЛК менеджера.

Модуль намеренно только читает канонические складские и биллинговые таблицы.
Никаких резервов, статусов, остатков или финансовых документов он не меняет.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.utils import timezone

from accountant.selectors import manager_visible_agencies
from billing.models import ApplicationCharge, BillingStorageDay, ClientInvoice, InvoicePayment
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseOperation, WarehouseOperationTask, WarehouseStockSnapshot
from sku.models import SKU

from .product_reports import ProductReportData, _csv_bytes, _xlsx_bytes, build_product_report
from .report_catalog import PRODUCT_REPORTS


PRODUCT_ALIASES = {
    ("clients", "client-stock"): "product-stock",
    ("clients", "client-movement"): "product-movement",
    ("clients", "client-slow-stock"): "idle-products",
    ("clients", "client-discrepancies"): "product-income",
    ("nomenclature", "sku-stock"): "product-stock",
    ("nomenclature", "sku-movement"): "product-movement",
    ("nomenclature", "expiration-dates"): "expiration-dates",
    ("nomenclature", "idle-nomenclature"): "idle-products",
    ("nomenclature", "weight-volume"): "product-box-flow",
    ("warehouse", "current-stock"): "product-stock",
    ("warehouse", "stock-by-warehouse"): "product-stock",
    ("warehouse", "stock-by-zone"): "product-stock",
    ("warehouse", "stock-by-cell"): "product-cells",
    ("warehouse", "stock-by-client"): "product-stock",
    ("warehouse", "stock-by-product"): "product-stock",
    ("warehouse", "stock-movement"): "product-movement",
    ("warehouse", "warehouse-load"): "product-box-flow",
    ("warehouse", "transfers"): "product-transfers",
    ("warehouse", "idle-stock"): "idle-products",
    ("warehouse", "expiration-dates"): "expiration-dates",
    ("warehouse", "reserves"): "product-reserves",
    ("warehouse", "warehouse-operation-history"): "product-movement",
}
_PRODUCT_REPORT_BY_CODE = {report.code: report for report in PRODUCT_REPORTS}

CUSTOM_REPORTS = {
    ("clients", "client-summary"),
    ("clients", "client-operations"),
    ("clients", "client-debt"),
    ("nomenclature", "catalog"),
    ("nomenclature", "barcodes"),
    ("nomenclature", "duplicates"),
    ("nomenclature", "without-barcode"),
    ("nomenclature", "without-dimensions"),
    ("warehouse", "empty-cells"),
    ("warehouse", "blocked-cells"),
    ("operations", "employee-productivity"),
    ("operations", "employee-quality"),
    ("operations", "operation-sla"),
    ("operations", "overdue-operations"),
    ("operations", "unfinished-operations"),
    ("operations", "stale-operations"),
    ("operations", "warehouse-task-queue"),
    ("finance", "charges-by-client"),
    ("finance", "charges-by-service"),
    ("finance", "storage-cost"),
    ("finance", "invoices"),
    ("finance", "payments"),
    ("finance", "client-debt"),
    ("finance", "overdue-debt"),
    ("finance", "charge-corrections"),
}

ACTIVE_OPERATION_STATUSES = {
    WarehouseOperation.STATUS_CREATED,
    WarehouseOperation.STATUS_PLANNED,
    WarehouseOperation.STATUS_IN_PROGRESS,
    WarehouseOperation.STATUS_PARTIAL,
    WarehouseOperation.STATUS_BLOCKED,
}
ACTIVE_TASK_STATUSES = {
    WarehouseOperationTask.STATUS_CREATED,
    WarehouseOperationTask.STATUS_IN_PROGRESS,
}


def decision_report_available(report) -> bool:
    key = (report.section, report.code)
    return key in PRODUCT_ALIASES or key in CUSTOM_REPORTS


def build_decision_report(report, params, *, user=None, paginate: bool = True) -> ProductReportData:
    alias = PRODUCT_ALIASES.get((report.section, report.code))
    if alias:
        return build_product_report(_PRODUCT_REPORT_BY_CODE[alias], params, user=user, paginate=paginate)

    rows = _custom_rows(report.section, report.code, params)
    visible_columns, hidden_columns = _columns(report, params)
    rows = _sort_rows(rows, params.get("sort"), visible_columns)
    page_rows = rows
    page_obj = None
    page_range = []
    if paginate:
        paginator = Paginator(rows, _page_size(params.get("page_size")))
        page_obj = paginator.get_page(params.get("page") or 1)
        page_rows = list(page_obj.object_list)
        page_range = list(paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1))
    totals = _totals(rows, visible_columns)
    return ProductReportData(
        rows=rows,
        page_rows=page_rows,
        visible_columns=visible_columns,
        hidden_columns=hidden_columns,
        totals=totals,
        kpis=_kpis(rows, visible_columns, totals),
        generated_at=timezone.localtime(),
        row_count=len(rows),
        page_obj=page_obj,
        page_range=page_range,
    )


def export_decision_report_response(report, params, export_format: str, *, user=None):
    data = build_decision_report(report, params, user=user, paginate=False)
    if export_format == "csv":
        payload = _csv_bytes(data.visible_columns, data.rows)
        content_type = "text/csv; charset=utf-8"
    else:
        payload = _xlsx_bytes(data.visible_columns, data.rows)
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    filename = f"{report.section}_{report.code}_{timezone.localdate().isoformat()}.{export_format}"
    response = HttpResponse(payload, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response, data.row_count, payload, filename


def display_decision_cell(row: dict, code: str) -> str:
    value = row.get(code)
    if value is None:
        return "—"
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, Decimal):
        return f"{value:,.2f}".replace(",", " ").replace(".", ",")
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    text = str(value).strip()
    return text or "—"


def _custom_rows(section: str, code: str, params) -> list[dict]:
    if code == "client-summary":
        return _client_summary_rows(params)
    if code in {"client-debt", "client-debt-summary"} or (section == "finance" and code == "client-debt"):
        return _client_debt_rows(params)
    if code == "client-operations" or code in {"operation-sla", "overdue-operations", "unfinished-operations", "stale-operations"}:
        return _operation_rows(code, params)
    if section == "nomenclature":
        return _nomenclature_rows(code, params)
    if code in {"empty-cells", "blocked-cells"}:
        return _cell_rows(code, params)
    if code in {"employee-productivity", "employee-quality"}:
        return _employee_rows(params)
    if code == "warehouse-task-queue":
        return _task_rows(params)
    if code in {"charges-by-client", "charges-by-service"}:
        return _charge_rows(code, params)
    if code == "storage-cost":
        return _storage_rows(params)
    if code in {"invoices", "overdue-debt"}:
        return _invoice_rows(code, params)
    if code == "payments":
        return _payment_rows(params)
    if code == "charge-corrections":
        return _correction_rows(params)
    return []


def _visible_ids() -> list[int]:
    return list(manager_visible_agencies().values_list("id", flat=True))


def _agency_name(agency) -> str:
    return str(getattr(agency, "short_name", "") or getattr(agency, "agn_name", "") or f"Клиент {getattr(agency, 'id', '')}").strip()


def _user_name(user) -> str:
    if not user:
        return ""
    full_name = str(user.get_full_name() or "").strip()
    return full_name or str(getattr(user, "username", "") or "").strip()


def _parse_date(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date() if value else None
    except (TypeError, ValueError):
        return None


def _date_range(params):
    date_from = _parse_date(params.get("date_from"))
    date_to = _parse_date(params.get("date_to"))
    period = str(params.get("period") or "").strip()
    if not date_from and period in {"7", "14", "30", "60", "90"}:
        date_from = timezone.localdate() - timedelta(days=int(period))
    return date_from, date_to


def _client_filter(qs, params, field="client_id"):
    value = str(params.get("client_id") or "").strip()
    if value.isdigit():
        qs = qs.filter(**{field: int(value)})
    return qs


def _client_summary_rows(params):
    agencies = list(_client_filter(manager_visible_agencies(), params, "id").order_by("agn_name", "id"))
    ids = [agency.id for agency in agencies]
    sku = {row["agency_id"]: row["count"] for row in SKU.objects.filter(agency_id__in=ids, deleted=False).values("agency_id").annotate(count=Count("id"))}
    stock = {
        row["agency_id"]: row
        for row in WarehouseStockSnapshot.objects.filter(agency_id__in=ids, is_archived=False)
        .values("agency_id")
        .annotate(stock_qty=Sum("qty"), available_qty=Sum("available_qty"))
    }
    operations = {
        row["agency_id"]: row["count"]
        for row in WarehouseOperation.objects.filter(agency_id__in=ids, status__in=ACTIVE_OPERATION_STATUSES)
        .values("agency_id")
        .annotate(count=Count("id"))
    }
    charges = {
        row["client_id"]: row["total"] or Decimal("0")
        for row in ApplicationCharge.objects.filter(client_id__in=ids, is_excluded=False)
        .values("client_id")
        .annotate(total=Sum("total_amount"))
    }
    debts = {
        row["client_id"]: row["total"] or Decimal("0")
        for row in ClientInvoice.objects.filter(client_id__in=ids, debt_amount__gt=0)
        .values("client_id")
        .annotate(total=Sum("debt_amount"))
    }
    return [
        {
            "client_name": _agency_name(agency),
            "inn": agency.inn or "",
            "sku_count": sku.get(agency.id, 0),
            "stock_qty": int(stock.get(agency.id, {}).get("stock_qty") or 0),
            "available_qty": int(stock.get(agency.id, {}).get("available_qty") or 0),
            "active_operations": operations.get(agency.id, 0),
            "charges_total": charges.get(agency.id, Decimal("0")),
            "debt_total": debts.get(agency.id, Decimal("0")),
        }
        for agency in agencies
    ]


def _client_debt_rows(params):
    qs = ClientInvoice.objects.filter(client_id__in=_visible_ids()).select_related("client")
    qs = _client_filter(qs, params)
    date_from, date_to = _date_range(params)
    if date_from:
        qs = qs.filter(invoice_date__gte=date_from)
    if date_to:
        qs = qs.filter(invoice_date__lte=date_to)
    grouped = {}
    today = timezone.localdate()
    for invoice in qs.order_by("client_id", "due_date"):
        row = grouped.setdefault(
            invoice.client_id,
            {
                "client_name": _agency_name(invoice.client),
                "invoice_count": 0,
                "total_amount": Decimal("0"),
                "paid_amount": Decimal("0"),
                "debt_amount": Decimal("0"),
                "overdue_amount": Decimal("0"),
                "nearest_due_date": None,
            },
        )
        row["invoice_count"] += 1
        row["total_amount"] += invoice.total_amount or Decimal("0")
        row["paid_amount"] += invoice.paid_amount or Decimal("0")
        row["debt_amount"] += invoice.debt_amount or Decimal("0")
        if invoice.debt_amount and invoice.due_date < today:
            row["overdue_amount"] += invoice.debt_amount
        if invoice.debt_amount and (row["nearest_due_date"] is None or invoice.due_date < row["nearest_due_date"]):
            row["nearest_due_date"] = invoice.due_date
    return list(grouped.values())


def _operation_queryset(params):
    qs = WarehouseOperation.objects.filter(agency_id__in=_visible_ids()).select_related(
        "agency", "source_location", "destination_location", "requested_by"
    )
    qs = _client_filter(qs, params, "agency_id")
    date_from, date_to = _date_range(params)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    if params.get("status"):
        qs = qs.filter(status__icontains=str(params.get("status")).strip())
    if params.get("operation_type"):
        qs = qs.filter(operation_type__icontains=str(params.get("operation_type")).strip())
    return qs


def _location_name(location, fallback=""):
    if not location:
        return fallback or ""
    return str(location.display_name or location.location_code or location.zone_code or fallback or "").strip()


def _operation_rows(code, params):
    qs = _operation_queryset(params)
    now = timezone.now()
    if code == "overdue-operations":
        qs = qs.filter(status__in=ACTIVE_OPERATION_STATUSES, created_at__lt=now - timedelta(hours=24))
    elif code == "unfinished-operations":
        qs = qs.filter(status__in=ACTIVE_OPERATION_STATUSES)
    elif code == "stale-operations":
        qs = qs.filter(status__in=ACTIVE_OPERATION_STATUSES, updated_at__lt=now - timedelta(hours=12))
    rows = []
    for operation in qs.order_by("-updated_at")[:10000]:
        planned = int(operation.planned_qty or 0)
        done = int(operation.done_qty or 0)
        rows.append(
            {
                "operation_id": f"OP-{operation.id}",
                "client_name": _agency_name(operation.agency),
                "operation_type": operation.get_operation_type_display(),
                "status": operation.get_status_display(),
                "planned_qty": planned,
                "done_qty": done,
                "progress_percent": round(done * 100 / planned, 1) if planned else (100 if operation.status == WarehouseOperation.STATUS_DONE else 0),
                "source": _location_name(operation.source_location, operation.source_zone_code),
                "destination": _location_name(operation.destination_location, operation.destination_zone_code),
                "responsible": _user_name(operation.requested_by) or operation.assigned_executor_role,
                "created_at": operation.created_at,
                "updated_at": operation.updated_at,
            }
        )
    return rows


def _nomenclature_queryset(params):
    qs = SKU.objects.filter(agency_id__in=_visible_ids(), deleted=False).select_related("agency").prefetch_related("barcodes")
    qs = _client_filter(qs, params, "agency_id")
    product = str(params.get("product") or "").strip()
    if product:
        qs = qs.filter(Q(name__icontains=product) | Q(sku_code__icontains=product))
    sku = str(params.get("sku") or params.get("article") or "").strip()
    if sku:
        qs = qs.filter(sku_code__icontains=sku)
    barcode = str(params.get("barcode") or "").strip()
    if barcode:
        qs = qs.filter(barcodes__value__icontains=barcode)
    category = str(params.get("category") or "").strip()
    if category:
        qs = qs.filter(Q(tovar_category__icontains=category) | Q(type_tovar__icontains=category))
    return qs.distinct()


def _sku_row(sku, barcode=None):
    barcodes = list(sku.barcodes.all())
    primary = next((item.value for item in barcodes if item.is_primary), "") or (barcodes[0].value if barcodes else "")
    return {
        "client_name": _agency_name(sku.agency),
        "sku": sku.sku_code,
        "product_name": sku.name,
        "barcode": barcode or primary,
        "barcode_count": len(barcodes),
        "category": sku.tovar_category or sku.type_tovar or sku.vid_tovar or "",
        "brand": sku.brand or "",
        "weight_kg": sku.weight_kg,
        "dimensions": " × ".join(str(value) for value in (sku.length_mm, sku.width_mm, sku.height_mm) if value is not None),
        "updated_at": sku.updated_at,
    }


def _nomenclature_rows(code, params):
    skus = list(_nomenclature_queryset(params).order_by("agency__agn_name", "sku_code")[:15000])
    if code == "without-barcode":
        skus = [sku for sku in skus if not list(sku.barcodes.all())]
    elif code == "without-dimensions":
        skus = [sku for sku in skus if any(value is None or value <= 0 for value in (sku.weight_kg, sku.length_mm, sku.width_mm, sku.height_mm))]
    elif code == "duplicates":
        groups = defaultdict(list)
        for sku in skus:
            key = (sku.agency_id, " ".join((sku.name or "").lower().split()))
            if key[1]:
                groups[key].append(sku)
        skus = [sku for items in groups.values() if len(items) > 1 for sku in items]
    if code == "barcodes":
        return [_sku_row(sku, barcode.value) for sku in skus for barcode in sku.barcodes.all()]
    return [_sku_row(sku) for sku in skus]


def _cell_rows(code, params):
    locations = WarehouseLocation.objects.all()
    if code == "empty-cells":
        locations = locations.filter(is_active=True, is_topology_visible=True)
    else:
        locations = locations.filter(Q(is_active=False) | Q(is_topology_visible=False))
    if params.get("warehouse_id"):
        locations = locations.filter(warehouse_code__icontains=str(params.get("warehouse_id")).strip())
    if params.get("zone"):
        locations = locations.filter(zone_code__icontains=str(params.get("zone")).strip())
    if params.get("cell"):
        value = str(params.get("cell")).strip()
        locations = locations.filter(Q(location_code__icontains=value) | Q(display_name__icontains=value))
    location_list = list(locations.order_by("warehouse_code", "zone_code", "row_no", "section_no", "tier_no", "cell_no")[:10000])
    ids = [item.id for item in location_list]
    container_counts = {
        row["current_location_id"]: row["count"]
        for row in WarehouseContainer.objects.filter(current_location_id__in=ids, status=WarehouseContainer.STATUS_ACTIVE)
        .values("current_location_id")
        .annotate(count=Count("id"))
    }
    stock_counts = {
        row["location_id"]: int(row["qty"] or 0)
        for row in WarehouseStockSnapshot.objects.filter(location_id__in=ids, agency_id__in=_visible_ids(), is_archived=False)
        .values("location_id")
        .annotate(qty=Sum("qty"))
    }
    if code == "empty-cells":
        location_list = [item for item in location_list if not container_counts.get(item.id) and not stock_counts.get(item.id)]
    return [
        {
            "warehouse_name": location.warehouse_code,
            "zone": location.zone_code,
            "cell": _location_name(location),
            "location_type": location.get_zone_kind_display(),
            "capacity": location.capacity_containers,
            "containers": container_counts.get(location.id, 0),
            "stock_qty": stock_counts.get(location.id, 0),
            "status": "Активна" if location.is_active and location.is_topology_visible else "Недоступна",
            "updated_at": location.updated_at,
        }
        for location in location_list
    ]


def _task_queryset(params):
    qs = WarehouseOperationTask.objects.filter(operation__agency_id__in=_visible_ids()).select_related("operation__agency", "assigned_to")
    qs = _client_filter(qs, params, "operation__agency_id")
    date_from, date_to = _date_range(params)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    return qs


def _employee_rows(params):
    grouped = {}
    for task in _task_queryset(params).order_by("id").iterator(chunk_size=2000):
        responsible = task.assigned_to_name or _user_name(task.assigned_to) or "Не назначен"
        key = (responsible, task.executor_role or "—")
        row = grouped.setdefault(key, {"responsible": key[0], "role": key[1], "task_count": 0, "completed_count": 0, "failed_count": 0, "planned_qty": 0, "done_qty": 0, "duration_minutes": 0.0, "duration_count": 0})
        row["task_count"] += 1
        row["completed_count"] += int(task.status == WarehouseOperationTask.STATUS_DONE)
        row["failed_count"] += int(task.status == WarehouseOperationTask.STATUS_FAILED)
        row["planned_qty"] += int(task.qty_planned or 0)
        row["done_qty"] += int(task.qty_done or 0)
        if task.started_at and task.completed_at:
            row["duration_minutes"] += max((task.completed_at - task.started_at).total_seconds() / 60, 0)
            row["duration_count"] += 1
    rows = []
    for row in grouped.values():
        row["completion_percent"] = round(row["completed_count"] * 100 / row["task_count"], 1) if row["task_count"] else 0
        duration_minutes = row.pop("duration_minutes")
        duration_count = row.pop("duration_count")
        row["average_minutes"] = round(duration_minutes / duration_count, 1) if duration_count else 0
        rows.append(row)
    return rows


def _task_rows(params):
    now = timezone.now()
    qs = _task_queryset(params).filter(status__in=ACTIVE_TASK_STATUSES).order_by("created_at")[:10000]
    return [
        {
            "task_id": f"TASK-{task.id}",
            "operation_id": f"OP-{task.operation_id}",
            "client_name": _agency_name(task.operation.agency),
            "task_type": task.get_task_type_display(),
            "status": task.get_status_display(),
            "responsible": task.assigned_to_name or _user_name(task.assigned_to) or "Не назначен",
            "planned_qty": int(task.qty_planned or 0),
            "done_qty": int(task.qty_done or 0),
            "age_hours": round(max((now - task.created_at).total_seconds() / 3600, 0), 1),
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
        for task in qs
    ]


def _charge_queryset(params):
    qs = ApplicationCharge.objects.filter(client_id__in=_visible_ids()).select_related("client", "service")
    qs = _client_filter(qs, params)
    date_from, date_to = _date_range(params)
    if date_from:
        qs = qs.filter(billing_period__gte=date_from)
    if date_to:
        qs = qs.filter(billing_period__lte=date_to)
    return qs


def _charge_rows(code, params):
    qs = _charge_queryset(params)
    if code == "charges-by-client":
        grouped = qs.values("client_id", "client__short_name", "client__agn_name")
    else:
        grouped = qs.values("service_id", "service__name", "service_name_snapshot")
    rows = grouped.annotate(
        charge_count=Count("id"),
        quantity=Sum("quantity"),
        amount=Sum("amount"),
        vat_amount=Sum("vat_amount"),
        total_amount=Sum("total_amount"),
        disputed_count=Count("id", filter=Q(is_disputed=True)),
        excluded_count=Count("id", filter=Q(is_excluded=True)),
    )
    result = []
    for row in rows:
        if code == "charges-by-client":
            name = row.get("client__short_name") or row.get("client__agn_name") or f"Клиент {row.get('client_id')}"
        else:
            name = row.get("service__name") or row.get("service_name_snapshot") or f"Услуга {row.get('service_id')}"
        result.append({"group_name": name, **{key: row.get(key) for key in ("charge_count", "quantity", "amount", "vat_amount", "total_amount", "disputed_count", "excluded_count")}})
    return result


def _storage_rows(params):
    qs = BillingStorageDay.objects.filter(client_id__in=_visible_ids()).select_related("client")
    qs = _client_filter(qs, params)
    date_from, date_to = _date_range(params)
    if date_from:
        qs = qs.filter(day__gte=date_from)
    if date_to:
        qs = qs.filter(day__lte=date_to)
    return [
        {
            "day": item.day,
            "client_name": _agency_name(item.client),
            "pallet_count": item.pallet_count,
            "box_count": item.box_count,
            "sku_unit_count": item.sku_unit_count,
            "physical_volume_m3": item.physical_volume_m3,
            "amount": item.amount,
            "vat_amount": item.vat_amount,
            "status": item.get_status_display(),
        }
        for item in qs.order_by("-day", "client__agn_name")[:15000]
    ]


def _invoice_rows(code, params):
    qs = ClientInvoice.objects.filter(client_id__in=_visible_ids()).select_related("client")
    qs = _client_filter(qs, params)
    date_from, date_to = _date_range(params)
    if date_from:
        qs = qs.filter(invoice_date__gte=date_from)
    if date_to:
        qs = qs.filter(invoice_date__lte=date_to)
    today = timezone.localdate()
    if code == "overdue-debt":
        qs = qs.filter(debt_amount__gt=0, due_date__lt=today)
    return [
        {
            "number": invoice.number,
            "client_name": _agency_name(invoice.client),
            "invoice_date": invoice.invoice_date,
            "due_date": invoice.due_date,
            "total_amount": invoice.total_amount,
            "paid_amount": invoice.paid_amount,
            "debt_amount": invoice.debt_amount,
            "status": invoice.get_status_display(),
            "overdue_days": max((today - invoice.due_date).days, 0) if invoice.debt_amount else 0,
        }
        for invoice in qs.order_by("-invoice_date", "-id")[:15000]
    ]


def _payment_rows(params):
    qs = InvoicePayment.objects.filter(invoice__client_id__in=_visible_ids()).select_related("invoice__client")
    qs = _client_filter(qs, params, "invoice__client_id")
    date_from, date_to = _date_range(params)
    if date_from:
        qs = qs.filter(paid_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(paid_at__date__lte=date_to)
    return [
        {
            "paid_at": payment.paid_at,
            "invoice_number": payment.invoice.number,
            "client_name": _agency_name(payment.invoice.client),
            "amount": payment.amount,
            "status": payment.get_status_display(),
            "source": payment.source,
            "comment": payment.comment,
        }
        for payment in qs.order_by("-paid_at", "-id")[:15000]
    ]


def _correction_rows(params):
    qs = _charge_queryset(params).filter(
        Q(correction_of__isnull=False) | Q(is_manual_override=True) | Q(is_excluded=True) | Q(original_quantity__isnull=False)
    ).select_related("overridden_by", "excluded_by", "created_by")
    rows = []
    for charge in qs.order_by("-performed_at", "-id")[:15000]:
        kinds = []
        if charge.correction_of_id:
            kinds.append("Корректирующая строка")
        if charge.is_manual_override:
            kinds.append("Изменение цены")
        if charge.original_quantity is not None:
            kinds.append("Изменение количества")
        if charge.is_excluded:
            kinds.append("Исключение")
        responsible = charge.overridden_by or charge.excluded_by or charge.created_by
        rows.append(
            {
                "performed_at": charge.performed_at or charge.created_at,
                "client_name": _agency_name(charge.client),
                "service_name": charge.service_name_snapshot or charge.service.name,
                "amount": charge.total_amount,
                "correction_type": ", ".join(kinds),
                "reason": charge.correction_reason or charge.override_reason or charge.exclude_comment or charge.qty_change_comment,
                "responsible": _user_name(responsible),
            }
        )
    return rows


def _columns(report, params):
    selected = params.getlist("columns") if hasattr(params, "getlist") else []
    if not selected:
        selected = list(report.default_columns or ())
    if not selected:
        return list(report.columns), []
    visible = [column for column in report.columns if column.code in selected]
    hidden = [column for column in report.columns if column.code not in selected]
    return visible or list(report.columns), hidden


def _sort_rows(rows, requested, columns):
    allowed = {column.code for column in columns if column.sortable}
    key = str(requested or "").strip()
    descending = key.startswith("-")
    key = key.lstrip("-")
    if key not in allowed:
        return rows
    def value(row):
        item = row.get(key)
        return (item is None, item if item is not None else "")
    try:
        return sorted(rows, key=value, reverse=descending)
    except TypeError:
        return sorted(rows, key=lambda row: str(row.get(key) or ""), reverse=descending)


def _page_size(value):
    try:
        return int(value) if int(value) in {20, 50, 100, 200} else 50
    except (TypeError, ValueError):
        return 50


def _totals(rows, columns):
    totals = {}
    for column in columns:
        if column.type != "number":
            continue
        values = [row.get(column.code) for row in rows if isinstance(row.get(column.code), (int, float, Decimal))]
        if values:
            totals[column.code] = sum(values)
    return totals


def _kpis(rows, columns, totals):
    result = [{"label": "Строк", "value": len(rows)}]
    for column in columns:
        if column.code in totals and len(result) < 4:
            result.append({"label": column.title, "value": display_decision_cell(totals, column.code)})
    return result
