from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import BytesIO, StringIO
from typing import Iterable
from urllib.parse import quote

from django.core.paginator import Paginator
from django.db.models import Min, Q, Sum
from django.http import HttpResponse
from django.utils import timezone

from accountant.selectors import manager_visible_agencies
from fbs.models import FbsPickScanEvent, FbsStockBalance
from shipping.models import ShippingOrder, ShippingOrderItem
from sklad.models import WarehouseEvent, WarehouseReserve, WarehouseStockSnapshot
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_stock_rows import normalize_stock_row_from_snapshot


NUMERIC_COLUMNS = {
    "actual_quantity",
    "available_quantity",
    "reserved_quantity",
    "blocked_quantity",
    "operation_quantity",
    "incoming_quantity",
    "outgoing_quantity",
    "accepted_quantity",
    "shortage_quantity",
    "surplus_quantity",
    "defect_quantity",
    "quantity",
    "current_quantity",
    "average_stock",
    "period_outcome",
    "shipping_count",
    "turnover_rate",
    "average_storage_days",
    "days_without_movement",
    "order_count",
    "operation_count",
    "volume_share",
    "trend_percent",
    "minimal_quantity",
    "shortage",
    "active_order_count",
    "recommended_replenishment",
    "average_outcome",
    "normative_stock",
    "excess_quantity",
    "forecast_storage_days",
    "days_to_expire",
    "occupied_volume",
    "available_volume",
    "fill_percent",
    "reserved_used",
    "reserve_balance",
    "unit_cost",
    "total_cost",
    "rank",
    "number",
    "width_mm",
    "height_mm",
    "depth_mm",
    "volume_m3",
    "weight_g",
}

DATE_COLUMNS = {
    "operation_at",
    "income_at",
    "date",
    "created_at",
    "expires_at",
    "income_at",
    "outcome_at",
    "last_movement_at",
    "last_income_at",
    "last_outcome_at",
    "manufactured_at",
    "expiration_at",
    "last_putaway_at",
    "valuation_at",
}

ALL_PRODUCT_REPORT_CODES = {
    "product-stock",
    "product-movement",
    "product-income-outcome-stock",
    "product-box-flow",
    "product-income",
    "product-outcome",
    "product-transfers",
    "turnover",
    "popular-products",
    "idle-products",
    "shortage-products",
    "excess-stock",
    "expiration-dates",
    "product-cells",
    "product-reserves",
    "stock-value",
}

INCOME_EVENTS = {"receiving_arrived", "placement_completed", "putaway_completed", "stock_returned_to_storage"}
OUTCOME_EVENTS = {
    "processing_consumed",
    "shipping_reserved",
    "shipped",
    "loaded_to_vehicle",
    "manual_writeoff",
    "warehouse_context_canceled",
}
TRANSFER_EVENTS = {
    "movement_requested",
    "movement_task_created",
    "movement_started",
    "movement_completed",
    "movement_canceled",
    "putaway_requested",
    "putaway_completed",
    "otg_requested",
    "otg_arrived",
    "ready_for_loading",
    "assigned_to_trip",
    "loading_started",
    "loaded_to_vehicle",
}
BOX_FLOW_INCOME_EVENTS = {
    "receiving_arrived",
    "placement_completed",
    "putaway_completed",
    "stock_returned_to_storage",
}
BOX_FLOW_OUTCOME_EVENTS = {
    "otg_arrived",
    "ready_for_loading",
    "assigned_to_trip",
    "loading_started",
    "loaded_to_vehicle",
    "shipped",
}
BOX_FLOW_HEADERS = (
    "№",
    "Клиент",
    "Паллет №",
    "Короб №",
    "SKU",
    "Номенклатура",
    "Штрихкод",
    "Количество, шт",
    "Статус",
    "Ширина, мм",
    "Высота, мм",
    "Глубина, мм",
    "Объем, м³",
    "Вес, г",
    "Приход дата",
    "Номер приходного документа",
    "Расход дата",
    "Номер расходного документа",
)
BOX_FLOW_EXPORT_COLUMNS = (
    "number",
    "client_name",
    "pallet_code",
    "box_code",
    "sku",
    "product_name",
    "barcode",
    "quantity",
    "status",
    "width_mm",
    "height_mm",
    "depth_mm",
    "volume_m3",
    "weight_g",
    "income_at",
    "income_document",
    "outcome_at",
    "outcome_document",
)
SOURCE_DOCUMENT_LABELS = {
    "receiving": "Приёмка",
    "receiving_placement": "Акт размещения приёмки",
    "receiving_flow_pallet": "Паллета приёмки",
    "receiving_correction": "Корректировка приёмки",
    "placement_act": "Акт размещения",
    "shipping": "Отгрузка",
    "shipping_order": "Заказ на отгрузку",
    "shipping_order_manual_restore": "Восстановление заказа на отгрузку",
    "shipping_correction": "Корректировка отгрузки",
    "processing": "Обработка",
    "processing_obr_request": "Заявка на обработку",
    "processing_placement": "Размещение после обработки",
    "processing_pallet_release": "Освобождение паллеты обработки",
    "processing_discrepancy_approval": "Согласование расхождения обработки",
    "reachtruck_processing_task": "Задание рич-траку",
    "fbs_plan": "План FBS",
    "fbs_movement": "Перемещение FBS",
    "fbs_client_movement": "Клиентское перемещение FBS",
    "fbs_replenishment": "Пополнение FBS",
    "fbs_pick_restock": "Возврат отбора FBS",
    "fbs_pallet": "Паллета FBS",
    "inventory_correction": "Корректировка инвентаризации",
    "inventory_reconciliation": "Сверка инвентаризации",
    "box_move_verification": "Проверка переноса короба",
    "manual_writeoff": "Ручное списание",
    "manual_stock_restore": "Восстановление остатка",
    "manual_stock_released": "Восстановление остатка",
    "inventory_balance_increased": "Корректировка учёта: приход",
    "inventory_balance_decreased": "Корректировка учёта: расход",
}

STOCK_CONTEXT_LABELS = {
    "receiving": "Приёмка",
    "shipping": "Отгрузка",
    "shipping_correction": "Корректировка отгрузки",
    "processing": "Обработка",
    "inventory": "Инвентаризация",
    "inventory_import": "Импорт остатков",
    "fbs_replenishment": "Пополнение FBS",
    "fbs_client_movement": "Клиентское перемещение FBS",
    "fbs_pick_restock": "Возврат отбора FBS",
    "fbs_pallet": "Паллета FBS",
    "stock_editor": "Редактор остатков",
    "missing_box_check": "Проверка недостачи короба",
    "warehouse_container": "Складская тара",
    "manual_repair": "Ручное исправление",
    "manual_writeoff": "Ручное списание",
}

EVENT_LABELS = {
    "receiving_arrived": "Принято на приёмке",
    "placement_started": "Размещение начато",
    "placement_completed": "Размещено на складе",
    "putaway_requested": "Задание на размещение",
    "putaway_completed": "Размещено на складе",
    "movement_requested": "Заявка на перемещение",
    "movement_task_created": "Задание на перемещение",
    "movement_started": "Перемещение начато",
    "movement_completed": "Перемещение выполнено",
    "movement_canceled": "Перемещение отменено",
    "palletization_started": "Паллетизация начата",
    "palletization_completed": "Паллетизация завершена",
    "otg_requested": "Запрошена отгрузка",
    "otg_arrived": "Прибыл в зону отгрузки",
    "ready_for_loading": "Готов к погрузке",
    "assigned_to_trip": "Назначен в рейс",
    "loading_started": "Погрузка начата",
    "loaded_to_vehicle": "Погружен в машину",
    "shipped": "Отгружен",
    "shipping_reserved": "Резерв под отгрузку",
    "shipping_reserve_released": "Резерв отгрузки снят",
    "shipping_packing_pallet_removed_from_order": "Паллета убрана из заказа",
    "processing_requested": "Заявка на обработку",
    "processing_reserved": "Резерв под обработку",
    "processing_reserve_released": "Резерв обработки снят",
    "processing_zone_arrived": "Передан в обработку",
    "processing_started": "Обработка начата",
    "processing_completed": "Обработка завершена",
    "processing_consumed": "Списан в обработку",
    "stock_returned_to_storage": "Возвращён на хранение",
    "warehouse_context_canceled": "Складской контекст отменён",
    "stock_corrected": "Остаток скорректирован",
    "nomenclature_corrected": "Номенклатура исправлена",
    "container_archived_reconciliation": "Тара архивирована при сверке",
    "missing_box_reported": "Заявлена недостача короба",
    "manual_writeoff": "Ручное списание",
    "stock_editor_box_moved_to_pallet": "Короб перенесён на паллету",
    "stock_editor_box_moved_to_new_pallet": "Короб перенесён на новую паллету",
    "stock_editor_pallet_renamed": "Паллета переименована",
    "fbs_replenishment_reserved": "Пополнение FBS: резерв",
    "fbs_replenishment_destination_selected": "Пополнение FBS: выбрано место",
    "fbs_replenishment_staged": "Пополнение FBS: подготовлено",
    "fbs_replenishment_collected": "Пополнение FBS: собрано",
    "fbs_replenishment_box_printed": "Пополнение FBS: короб распечатан",
    "fbs_replenishment_box_closed": "Пополнение FBS: короб закрыт",
    "fbs_replenishment_completed": "Пополнение FBS выполнено",
    "fbs_replenishment_canceled": "Пополнение FBS отменено",
    "fbs_box_placement_completed": "Короб FBS размещён",
    "fbs_movement_reserved": "Перемещение FBS: резерв",
    "fbs_movement_reserve_released": "Перемещение FBS: резерв снят",
    "fbs_full_pallet_moved": "Паллета FBS перемещена целиком",
    "fbs_free_relocation_started": "Свободное перемещение FBS начато",
    "fbs_free_relocation_completed": "Свободное перемещение FBS завершено",
    "fbs_physical_location_corrected": "Физическое место FBS исправлено",
    "fbs_pick_restocked": "Возврат отбора FBS",
    "fbs_missing_box_reported": "Заявлена недостача короба FBS",
    "fbs_missing_box_skipped": "Недостача короба FBS пропущена",
    "fbs_missing_box_automatic_quarantine_released": "Карантин недостачи короба FBS снят",
    "fbs_client_movement_stock_export_activated": "Клиентское перемещение FBS активировано",
}

DIRECTION_LABELS = {
    "income": "Приход",
    "outcome": "Расход",
    "transfer": "Перемещение",
    "reserve": "Резерв",
    "service": "Служебное",
}

# Приход — товар появился на складе; расход — покинул склад или списан.
# Всё, что двигает товар между зонами и контурами, остаётся перемещением.
MOVEMENT_INCOME_EVENTS = {
    "receiving_arrived",
    "placement_started",
    "placement_completed",
    "putaway_requested",
    "putaway_completed",
    "stock_returned_to_storage",
    "fbs_box_placement_completed",
    "manual_stock_restore",
    "manual_stock_released",
    "inventory_balance_increased",
}
MOVEMENT_OUTCOME_EVENTS = {
    "shipped",
    "loaded_to_vehicle",
    "manual_writeoff",
    "processing_consumed",
    "inventory_balance_decreased",
}
MOVEMENT_BALANCE_INCOME_EVENTS = {
    "placement_completed",
    "manual_stock_restore",
    "manual_stock_released",
    "inventory_balance_increased",
}
MOVEMENT_BALANCE_OUTCOME_EVENTS = {
    "shipped",
    "processing_consumed",
    "manual_writeoff",
    "inventory_balance_decreased",
}
MOVEMENT_BALANCE_EVENTS = MOVEMENT_BALANCE_INCOME_EVENTS | MOVEMENT_BALANCE_OUTCOME_EVENTS
MOVEMENT_RESERVE_EVENTS = {
    "shipping_reserved",
    "shipping_reserve_released",
    "processing_reserved",
    "processing_reserve_released",
    "fbs_movement_reserved",
    "fbs_movement_reserve_released",
    "fbs_replenishment_reserved",
}
MOVEMENT_TRANSFER_EVENTS = {
    "movement_requested",
    "movement_task_created",
    "movement_started",
    "movement_completed",
    "movement_canceled",
    "palletization_started",
    "palletization_completed",
    "otg_requested",
    "otg_arrived",
    "ready_for_loading",
    "assigned_to_trip",
    "loading_started",
    "processing_zone_arrived",
    "processing_started",
    "processing_completed",
    "fbs_replenishment_destination_selected",
    "fbs_replenishment_staged",
    "fbs_replenishment_collected",
    "fbs_replenishment_completed",
    "fbs_full_pallet_moved",
    "fbs_free_relocation_started",
    "fbs_free_relocation_completed",
    "fbs_pick_restocked",
    "stock_editor_box_moved_to_pallet",
    "stock_editor_box_moved_to_new_pallet",
}

MOVEMENT_CONTAINER_LIMIT = 2000


class MovementSelectionTooBroad(Exception):
    """Фильтр по товару задевает слишком много коробов, отчёт не строим."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(
            f"Под фильтр по товару попало более {MOVEMENT_CONTAINER_LIMIT} коробов "
            f"(найдено не меньше {count}). Уточните клиента, артикул или период."
        )


RESERVE_ACTIVE_STATUSES = {
    WarehouseReserve.STATUS_ACTIVE,
    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
    WarehouseReserve.STATUS_ALLOCATED,
    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
}


@dataclass(frozen=True)
class ProductReportData:
    rows: list[dict]
    page_rows: list[dict]
    visible_columns: list
    hidden_columns: list
    totals: dict
    kpis: list[dict]
    generated_at: datetime
    row_count: int
    page_obj: object | None
    page_range: list[int]
    error_message: str = ""
    unavailable_message: str = ""
    extra: dict = field(default_factory=dict)


def product_report_available(report_code: str) -> bool:
    return report_code in ALL_PRODUCT_REPORT_CODES


def build_product_report(report, params, *, user=None, paginate: bool = True) -> ProductReportData:
    extra: dict = {}
    if report.code == "product-income-outcome-stock":
        rows, unavailable_message, extra = _product_balance_report(params)
    elif report.code == "product-movement":
        rows, unavailable_message, extra = _product_movement_report(params)
    else:
        rows, unavailable_message = _rows_for_report(report.code, params, user=user)
    rows = _sort_rows(rows, params.get("sort") or _default_sort(report.code))
    visible_columns, hidden_columns = _column_specs(report, params)
    row_count = len(rows)
    page_obj = None
    page_rows = rows
    page_range: list[int] = []
    if paginate:
        page_size = _page_size(params.get("page_size"))
        paginator = Paginator(rows, page_size)
        page_obj = paginator.get_page(params.get("page") or 1)
        page_rows = list(page_obj.object_list)
        page_range = list(paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1))
    return ProductReportData(
        rows=rows,
        page_rows=page_rows,
        visible_columns=visible_columns,
        hidden_columns=hidden_columns,
        totals=_totals(rows),
        kpis=(
            _product_balance_kpis(extra)
            if report.code == "product-income-outcome-stock"
            else _movement_kpis(rows, extra)
            if report.code == "product-movement"
            else _kpis(rows)
        ),
        generated_at=timezone.localtime(),
        row_count=row_count,
        page_obj=page_obj,
        page_range=page_range,
        unavailable_message=unavailable_message,
        extra=extra,
    )


def export_product_report_response(report, params, export_format: str, *, user=None) -> tuple[HttpResponse, int, bytes, str]:
    data = build_product_report(report, params, user=user, paginate=False)
    columns = data.visible_columns
    report_date = timezone.localdate().isoformat()
    sku = str(params.get("sku") or params.get("article") or "").strip()
    if report.code == "product-income-outcome-stock" and sku:
        safe_sku = "".join("_" if char in '\\/:*?\"<>|' or ord(char) < 32 else char for char in sku)
        safe_sku = safe_sku.strip(" ._")[:80] or "product_balance"
        filename = f"{safe_sku}_{report_date}.{export_format}"
        ascii_filename = f"product_balance_{report_date}.{export_format}"
    else:
        filename = f"{report.section}_{report.code}_{report_date}.{export_format}"
        ascii_filename = filename
    if export_format == "csv":
        payload = _csv_bytes(columns, data.rows)
        response = HttpResponse(payload, content_type="text/csv; charset=utf-8")
    else:
        if report.code == "product-box-flow":
            payload = _box_flow_xlsx_bytes(data.rows)
        elif report.code == "product-income-outcome-stock":
            payload = _product_balance_xlsx_bytes(data)
        else:
            payload = _xlsx_bytes(columns, data.rows)
        response = HttpResponse(
            payload,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    response["Content-Disposition"] = (
        f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quote(filename)}'
    )
    return response, data.row_count, payload, filename


def display_cell(row: dict, code: str) -> str:
    value = row.get(code)
    if value is None:
        return "—"
    if code in DATE_COLUMNS:
        if hasattr(value, "strftime"):
            if getattr(value, "hour", 0) or getattr(value, "minute", 0):
                return timezone.localtime(value).strftime("%d.%m.%Y %H:%M") if timezone.is_aware(value) else value.strftime("%d.%m.%Y %H:%M")
            return value.strftime("%d.%m.%Y")
        return str(value) or "—"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    text = str(value).strip()
    return text or "—"


def _csv_bytes(columns, rows: list[dict]) -> bytes:
    buffer = StringIO()
    buffer.write("\ufeff")
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([column.title for column in columns])
    for row in rows:
        writer.writerow([display_cell(row, column.code) for column in columns])
    return buffer.getvalue().encode("utf-8")


def _xlsx_bytes(columns, rows: list[dict]) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Отчёт"
    sheet.append([column.title for column in columns])
    for row in rows:
        sheet.append([display_cell(row, column.code) for column in columns])
    for column_cells in sheet.columns:
        values = [str(cell.value or "") for cell in column_cells]
        width = min(max(len(value) for value in values) + 2, 42) if values else 12
        sheet.column_dimensions[column_cells[0].column_letter].width = width
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _box_flow_xlsx_bytes(rows: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Отчет"
    sheet.append(list(BOX_FLOW_HEADERS))
    header_fill = PatternFill("solid", fgColor="FFF2CC")
    header_font = Font(name="Calibri", size=11, bold=True)
    body_font = Font(name="Calibri", size=11)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
    for row in rows:
        sheet.append([_box_flow_excel_value(row.get(code), code) for code in BOX_FLOW_EXPORT_COLUMNS])
        for cell in sheet[sheet.max_row]:
            cell.font = body_font
    widths = {
        1: 8,
        2: 16,
        3: 26,
        4: 26,
        5: 14,
        6: 30,
        7: 18,
        8: 15,
        9: 18,
        10: 12,
        11: 12,
        12: 12,
        13: 12,
        14: 12,
        15: 14,
        16: 24,
        17: 14,
        18: 26,
    }
    for index, width in widths.items():
        sheet.column_dimensions[get_column_letter(index)].width = width
    for row_idx in range(2, sheet.max_row + 1):
        sheet.cell(row_idx, 7).number_format = "@"
        sheet.cell(row_idx, 13).number_format = "0.000000"
        sheet.cell(row_idx, 15).number_format = "m/d/yy"
        sheet.cell(row_idx, 17).number_format = "m/d/yy"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:R{max(sheet.max_row, 1)}"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _box_flow_excel_value(value, code: str):
    if value in (None, ""):
        return None
    if code in {"income_at", "outcome_at"}:
        if hasattr(value, "date"):
            return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
        return value
    return value


def _product_balance_xlsx_bytes(data: ProductReportData) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "Сводка"
    summary = data.extra.get("summary") or {}
    summary_rows = (
        ("Клиент", summary.get("client_name") or ""),
        ("Артикул / SKU", summary.get("sku") or ""),
        ("Товар", summary.get("product_name") or ""),
        ("Период движения", summary.get("period_label") or "За всё время"),
        ("Остаток на начало периода (расчётный)", int(summary.get("period_start_quantity") or 0)),
        ("Приход от клиента", int(summary.get("incoming_quantity") or 0)),
        ("Восстановлено корректировкой", int(summary.get("restored_quantity") or 0)),
        ("Корректировка прихода", int(summary.get("adjustment_incoming_quantity") or 0)),
        ("Приход всего", int(summary.get("incoming_total_quantity") or 0)),
        (
            "Размещено после обработки (внутреннее движение)",
            int(summary.get("processing_placement_quantity") or 0),
        ),
        ("Отгружено со склада", int(summary.get("shipped_quantity") or 0)),
        ("Отобрано в заказы FBS", int(summary.get("fbs_picked_quantity") or 0)),
        ("Списано в обработке", int(summary.get("processing_consumed_quantity") or 0)),
        ("Ручное списание", int(summary.get("manual_writeoff_quantity") or 0)),
        ("Корректировка расхода", int(summary.get("adjustment_outgoing_quantity") or 0)),
        ("Расход всего", int(summary.get("outgoing_quantity") or 0)),
        ("Остаток на конец периода (расчётный)", int(summary.get("period_end_quantity") or 0)),
        ("Текущий остаток всего", int(summary.get("physical_quantity") or 0)),
        ("Текущий остаток: общий склад", int(summary.get("warehouse_physical_quantity") or 0)),
        ("Текущий остаток: FBS", int(summary.get("fbs_physical_quantity") or 0)),
        ("Общий склад: хранение OS", int(summary.get("warehouse_os_quantity") or 0)),
        ("Общий склад: отгрузка OTG", int(summary.get("warehouse_otg_quantity") or 0)),
        ("Общий склад: обработка OBR", int(summary.get("warehouse_obr_quantity") or 0)),
        ("Активный резерв", int(summary.get("reserved_quantity") or 0)),
        ("Доступно", int(summary.get("available_quantity") or 0)),
        ("Контроль: начало + приход − расход − конец", int(summary.get("control_balance") or 0)),
        ("Проверка данных", summary.get("balance_status") or ""),
        (
            "Методика",
            "Остаток включает общий склад и FBS; отбор FBS считается расходом из товарного остатка.",
        ),
        ("Сформирован", timezone.localtime(data.generated_at).strftime("%d.%m.%Y %H:%M")),
    )
    summary_sheet.append(["Показатель", "Значение"])
    for row in summary_rows:
        summary_sheet.append(list(row))

    movement_sheet = workbook.create_sheet("Движения")
    movement_columns = (
        ("operation_at", "Дата и время"),
        ("operation_type", "Операция"),
        ("document_number", "Документ"),
        ("product_name", "Товар"),
        ("article", "Артикул"),
        ("incoming_quantity", "Приход"),
        ("outgoing_quantity", "Расход"),
        ("pallet_code", "Паллета"),
        ("box_code", "Короб"),
        ("location", "Место"),
        ("responsible", "Ответственный"),
    )
    movement_sheet.append([title for _, title in movement_columns])
    for row in data.rows:
        movement_sheet.append([_product_balance_excel_value(row.get(code), code) for code, _ in movement_columns])

    stock_sheet = workbook.create_sheet("Текущий остаток")
    stock_columns = (
        ("stock_area", "Контур"),
        ("zone_code", "Зона"),
        ("pallet_code", "Паллета"),
        ("box_code", "Короб"),
        ("location", "Место"),
        ("quantity", "Физический остаток"),
        ("processing_reserved_quantity", "Резерв обработки"),
        ("shipping_reserved_quantity", "Резерв отгрузки"),
        ("other_reserved_quantity", "Прочий резерв"),
        ("available_quantity", "Доступно"),
        ("status", "Статус"),
    )
    stock_sheet.append([title for _, title in stock_columns])
    for row in data.extra.get("stock_rows") or []:
        stock_sheet.append([_product_balance_excel_value(row.get(code), code) for code, _ in stock_columns])

    reserve_sheet = workbook.create_sheet("Активные резервы")
    reserve_columns = (
        ("reserve_number", "Резерв №"),
        ("created_at", "Создан"),
        ("reserve_type", "Тип"),
        ("document_number", "Документ"),
        ("reserved_quantity", "Зарезервировано"),
        ("satisfied_quantity", "Исполнено"),
        ("reserve_balance", "Остаток резерва"),
        ("status", "Статус"),
    )
    reserve_sheet.append([title for _, title in reserve_columns])
    for row in data.extra.get("reserve_rows") or []:
        reserve_sheet.append([_product_balance_excel_value(row.get(code), code) for code, _ in reserve_columns])

    header_fill = PatternFill("solid", fgColor="F8B800")
    header_font = Font(name="Calibri", size=11, bold=True, color="303030")
    for sheet in workbook.worksheets:
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for column_cells in sheet.columns:
            values = [str(cell.value or "") for cell in column_cells]
            width = min(max(len(value) for value in values) + 2, 48) if values else 12
            sheet.column_dimensions[column_cells[0].column_letter].width = max(width, 12)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _product_balance_excel_value(value, code: str):
    if value in (None, ""):
        return None
    if code in {"operation_at", "created_at"} and hasattr(value, "tzinfo"):
        localized = timezone.localtime(value) if timezone.is_aware(value) else value
        return localized.replace(tzinfo=None)
    return value


def _column_specs(report, params):
    raw_selected = params.getlist("columns") if hasattr(params, "getlist") else params.get("columns", [])
    if isinstance(raw_selected, str):
        raw_selected = [raw_selected]
    selected = [str(value).strip() for value in raw_selected if str(value).strip()]
    columns = list(report.columns)
    if not selected:
        default_codes = set(getattr(report, "default_columns", ()) or ())
        if default_codes:
            visible = [column for column in columns if column.code in default_codes]
            if visible:
                return visible, [column for column in columns if column.code not in default_codes]
        return columns, []
    selected_set = set(selected)
    visible = [column for column in columns if column.code in selected_set]
    return visible or columns, [column for column in columns if column.code not in selected_set]


def _page_size(value) -> int:
    try:
        size = int(value or 50)
    except (TypeError, ValueError):
        return 50
    return min(max(size, 20), 200)


def _default_sort(report_code: str) -> str:
    return {
        "product-movement": "-operation_at",
        "product-income-outcome-stock": "-operation_at",
        "product-income": "-income_at",
        "product-outcome": "-date",
        "product-transfers": "-date",
        "popular-products": "rank",
        "idle-products": "-days_without_movement",
        "shortage-products": "-shortage",
        "excess-stock": "-excess_quantity",
        "expiration-dates": "days_to_expire",
        "product-reserves": "-created_at",
    }.get(report_code, "product_name")


def _sort_rows(rows: list[dict], sort_code: str) -> list[dict]:
    sort_code = str(sort_code or "").strip()
    if not sort_code:
        return rows
    reverse = sort_code.startswith("-")
    key = sort_code[1:] if reverse else sort_code
    if not rows or key not in rows[0]:
        return rows

    def _value(row):
        value = row.get(key)
        if value is None:
            return (1, "")
        if hasattr(value, "timestamp"):
            return (0, value.timestamp())
        if hasattr(value, "toordinal"):
            return (0, value.toordinal())
        if isinstance(value, (int, float)):
            return (0, value)
        return (0, str(value).casefold())

    return sorted(rows, key=_value, reverse=reverse)


def _totals(rows: list[dict]) -> dict:
    totals: dict[str, int | float] = {}
    for row in rows:
        for key in NUMERIC_COLUMNS:
            value = row.get(key)
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
    return totals


def _kpis(rows: list[dict]) -> list[dict]:
    totals = _totals(rows)
    return [
        {"label": "Строк", "value": len(rows)},
        {"label": "Общий остаток", "value": int(totals.get("actual_quantity") or totals.get("current_quantity") or totals.get("quantity") or 0)},
        {"label": "Резерв", "value": int(totals.get("reserved_quantity") or 0)},
        {"label": "Доступно", "value": int(totals.get("available_quantity") or 0)},
    ]


def _product_balance_kpis(extra: dict) -> list[dict]:
    summary = extra.get("summary") or {}
    period_suffix = " за период" if summary.get("period_limited") else ""
    control_balance = summary.get("control_balance")
    return [
        {"label": "Остаток на начало", "value": int(summary.get("period_start_quantity") or 0)},
        {"label": f"Приход{period_suffix}", "value": int(summary.get("incoming_total_quantity") or 0)},
        {"label": f"Расход{period_suffix}", "value": int(summary.get("outgoing_quantity") or 0)},
        {"label": "Остаток на конец", "value": int(summary.get("period_end_quantity") or 0)},
        {"label": "Активный резерв", "value": int(summary.get("reserved_quantity") or 0)},
        {"label": "Доступно", "value": int(summary.get("available_quantity") or 0)},
        {"label": "Контрольный баланс", "value": int(control_balance or 0)},
    ]


def _movement_balance_totals(rows: list[dict]) -> tuple[int, int, int]:
    incoming_quantity = 0
    outgoing_quantity = 0
    transfer_count = 0
    for row in rows:
        event_code = str(row.get("event_code") or "").strip()
        quantity = row.get("operation_quantity")
        quantity = int(quantity) if isinstance(quantity, (int, float)) else 0
        if event_code in MOVEMENT_BALANCE_INCOME_EVENTS:
            incoming_quantity += quantity
        elif event_code in MOVEMENT_BALANCE_OUTCOME_EVENTS:
            outgoing_quantity += quantity
        if row.get("direction") == "Перемещение":
            transfer_count += 1
    return incoming_quantity, outgoing_quantity, transfer_count


def _movement_period_balance(card: dict, period_rows: list[dict], future_rows: list[dict]) -> dict:
    incoming_quantity, outgoing_quantity, _ = _movement_balance_totals(period_rows)
    future_incoming, future_outgoing, _ = _movement_balance_totals(future_rows)
    current_quantity = int(card.get("current_quantity") or 0)
    period_end_quantity = current_quantity - future_incoming + future_outgoing
    period_start_quantity = period_end_quantity - incoming_quantity + outgoing_quantity
    return {
        **card,
        "period_incoming_quantity": incoming_quantity,
        "period_outgoing_quantity": outgoing_quantity,
        "period_start_quantity": period_start_quantity,
        "period_end_quantity": period_end_quantity,
    }


def _movement_kpis(rows: list[dict], extra: dict | None = None) -> list[dict]:
    incoming_quantity, outgoing_quantity, transfer_count = _movement_balance_totals(rows)

    card = (extra or {}).get("movement_card") or {}
    if (extra or {}).get("movement_card_enabled"):
        incoming_quantity = int(card.get("period_incoming_quantity") or incoming_quantity)
        outgoing_quantity = int(card.get("period_outgoing_quantity") or outgoing_quantity)
        return [
            {"label": "Остаток на начало (расчётный), шт", "value": int(card.get("period_start_quantity") or 0)},
            {"label": "Приход за период, шт", "value": incoming_quantity},
            {"label": "Расход за период, шт", "value": outgoing_quantity},
            {"label": "Остаток на конец (расчётный), шт", "value": int(card.get("period_end_quantity") or 0)},
            {"label": "Перемещений", "value": transfer_count},
            {"label": "Текущий остаток сейчас, шт", "value": int(card.get("current_quantity") or 0)},
            {"label": "Доступно сейчас", "value": int(card.get("available_quantity") or 0)},
            {"label": "Резерв сейчас", "value": int(card.get("reserved_quantity") or 0)},
        ]
    return [
        {"label": "Строк", "value": len(rows)},
        {"label": "Приход, шт", "value": incoming_quantity},
        {"label": "Расход, шт", "value": outgoing_quantity},
        {"label": "Перемещений", "value": transfer_count},
    ]


def _movement_product_filter(params) -> tuple[str, str]:
    for code in ("sku", "article", "product", "barcode"):
        value = str(params.get(code) or "").strip()
        if value:
            return code, value
    return "", ""


def _movement_card_sku(agency, filter_code: str, value: str) -> str:
    if filter_code in {"sku", "article"}:
        return value
    resolved = (
        WarehouseStockSnapshot.objects.filter(agency=agency)
        .filter(Q(sku_code__iexact=value) | Q(barcode__iexact=value) | Q(name__iexact=value))
        .order_by("id")
        .values_list("sku_code", flat=True)
        .first()
    )
    return str(resolved or value).strip()


def _movement_card_stock_summary(agency, sku: str) -> dict:
    current_quantity = 0
    reserved_quantity = 0
    available_quantity = 0
    for row in StockAvailabilityService.stock_rows_with_availability(agency=agency, sku_values={sku}):
        if str(row.get("sku") or "").strip().casefold() != sku.casefold():
            continue
        state = str(row.get("warehouse_state_code") or "").strip().lower()
        if state in {"processing_consumed", "shipped"}:
            continue
        current_quantity += int(row.get("qty") or 0)
        reserved_quantity += (
            int(row.get("processing_reserved_qty") or 0)
            + int(row.get("shipping_reserved_qty") or 0)
            + int(row.get("other_reserved_qty") or 0)
        )
        available_quantity += int(row.get("available_qty") or 0)
    return {
        "sku": sku,
        "current_quantity": current_quantity,
        "reserved_quantity": reserved_quantity,
        "available_quantity": available_quantity,
    }


def _movement_journal_notice(*, balance_calculated: bool = False) -> str:
    started_at = WarehouseEvent.objects.aggregate(started_at=Min("occurred_at"))["started_at"]
    if started_at is None:
        return ""
    local_started_at = timezone.localtime(started_at) if timezone.is_aware(started_at) else started_at
    notice = (
        f"Журнал складских событий ведётся с {local_started_at:%d.%m.%Y} — "
        "движения до этой даты в отчёт не попадают."
    )
    if balance_calculated:
        notice += (
            " Начальный и конечный остатки восстановлены обратным расчётом от текущего остатка "
            "по зафиксированным приходам и расходам."
        )
    return notice


def _product_movement_report(params) -> tuple[list[dict], str, dict]:
    client_id = _int_or_none(params.get("client_id"))
    filter_code, filter_value = _movement_product_filter(params)
    card_enabled = bool(client_id and filter_value)
    visible_agencies = manager_visible_agencies()
    agency = None
    if client_id:
        agency = visible_agencies.filter(pk=client_id).first()
        if agency is None:
            return (
                [],
                "Клиент не найден среди доступных менеджеру клиентов.",
                {"movement_card_enabled": card_enabled, "movement_card": {}},
            )

    sku = ""
    extra = {"movement_card_enabled": card_enabled, "movement_card": {}}
    if card_enabled:
        sku = _movement_card_sku(agency, filter_code, filter_value)
        extra["movement_card"] = _movement_card_stock_summary(agency, sku)

    rows, unavailable_message = _guard_movement(
        lambda: _movement_rows(
            params,
            event_types=None,
            visible_agencies=visible_agencies,
            exact_product=card_enabled,
            product_value=sku,
        )
    )
    if unavailable_message:
        return rows, unavailable_message, extra
    if card_enabled:
        date_from, date_to = _date_range(params)
        future_rows = []
        if date_to and date_to < timezone.localdate():
            future_params = params.copy()
            future_params["date_from"] = (date_to + timedelta(days=1)).isoformat()
            future_params.pop("date_to", None)
            future_rows = _movement_rows(
                future_params,
                event_types=MOVEMENT_BALANCE_EVENTS,
                visible_agencies=visible_agencies,
                exact_product=True,
                product_value=sku,
            )
        extra["movement_card"] = _movement_period_balance(extra["movement_card"], rows, future_rows)
        extra["movement_card"]["date_from"] = date_from
        extra["movement_card"]["date_to"] = date_to
    return rows, _movement_journal_notice(balance_calculated=card_enabled), extra


def _guard_movement(builder) -> tuple[list[dict], str]:
    """Отчёты на журнале событий: предел выборки показываем подсказкой."""
    try:
        return builder(), ""
    except MovementSelectionTooBroad as exc:
        return [], str(exc)


def _rows_for_report(report_code: str, params, *, user=None) -> tuple[list[dict], str]:
    if report_code == "product-box-flow":
        return _box_flow_rows(params), ""
    if report_code == "product-income":
        return _guard_movement(lambda: _income_rows(params))
    if report_code == "product-outcome":
        return _guard_movement(lambda: _outcome_rows(params))
    if report_code == "product-transfers":
        return _guard_movement(lambda: _transfer_rows(params))
    if report_code == "turnover":
        return _turnover_rows(params), ""
    if report_code == "popular-products":
        return _popular_rows(params), ""
    if report_code == "idle-products":
        return _idle_rows(params), ""
    if report_code == "shortage-products":
        return _shortage_rows(params), ""
    if report_code == "excess-stock":
        return _excess_rows(params), ""
    if report_code == "expiration-dates":
        return _expiration_rows(params), ""
    if report_code == "product-cells":
        return _cell_rows(params), ""
    if report_code == "product-reserves":
        return _reserve_rows(params), ""
    if report_code == "stock-value":
        return _stock_value_rows(params), "В текущих моделях нет закупочной стоимости товара, поэтому стоимость единицы и общая стоимость не рассчитываются."
    return _stock_rows(params), ""


def _product_balance_report(params) -> tuple[list[dict], str, dict]:
    client_id = _int_or_none(params.get("client_id"))
    sku = str(params.get("sku") or params.get("article") or "").strip()
    if not client_id or not sku:
        return [], "Выберите клиента и укажите точный артикул / SKU.", {"summary": {}}
    agency = manager_visible_agencies().filter(pk=client_id).first()
    if agency is None:
        return [], "Клиент не найден среди доступных менеджеру клиентов.", {"summary": {}}

    date_from, date_to = _date_range(params)
    snapshot_qs = WarehouseStockSnapshot.objects.filter(agency=agency, sku_code__iexact=sku)
    snapshot_ids = list(snapshot_qs.values_list("id", flat=True))
    last_event_ids = list(snapshot_qs.exclude(last_event_id=None).values_list("last_event_id", flat=True))

    stock_rows = []
    for row in StockAvailabilityService.stock_rows_with_availability(agency=agency, sku_values={sku}):
        if str(row.get("sku") or "").strip().casefold() != sku.casefold():
            continue
        state = str(row.get("warehouse_state_code") or "").strip().lower()
        if state in {"processing_consumed", "shipped"}:
            continue
        zone_code = str(row.get("zone_code") or row.get("zone") or "").strip().upper()
        stock_rows.append(
            {
                "stock_area": "Общий склад",
                "zone_code": zone_code,
                "pallet_code": row.get("pallet_code") or "",
                "box_code": row.get("box_code") or "",
                "location": row.get("location") or row.get("zone") or "",
                "quantity": int(row.get("qty") or 0),
                "processing_reserved_quantity": int(row.get("processing_reserved_qty") or 0),
                "shipping_reserved_quantity": int(row.get("shipping_reserved_qty") or 0),
                "other_reserved_quantity": int(row.get("other_reserved_qty") or 0),
                "available_quantity": int(row.get("available_qty") or 0),
                "status": row.get("warehouse_state_code") or "",
                "product_name": row.get("name") or sku,
            }
        )

    fbs_balance_base_qs = FbsStockBalance.objects.filter(
        agency=agency,
        sku_code__iexact=sku,
    )
    fbs_balance_ids = list(fbs_balance_base_qs.values_list("id", flat=True))
    for balance in (
        fbs_balance_base_qs.filter(qty__gt=0)
        .select_related("box__pallet__cell__location")
        .order_by("box__pallet__cell__location__location_code", "box__box_code", "id")
    ):
        location = balance.box.pallet.cell.location
        stock_rows.append(
            {
                "stock_area": "FBS",
                "zone_code": "FBS",
                "pallet_code": balance.box.pallet.pallet_code,
                "box_code": balance.box.box_code,
                "location": _cell_label(location) or balance.box.pallet.cell.cell_code,
                "quantity": int(balance.qty or 0),
                "processing_reserved_quantity": 0,
                "shipping_reserved_quantity": int(balance.reserved_qty or 0),
                "other_reserved_quantity": 0,
                "available_quantity": int(balance.available_qty or 0),
                "status": "FBS",
                "product_name": balance.name or sku,
            }
        )
    stock_rows.sort(key=lambda row: (str(row["location"]), str(row["pallet_code"]), str(row["box_code"])))

    product_sku_match = (
        Q(payload__sku_code__iexact=sku)
        | Q(payload__sku__iexact=sku)
        | Q(payload__snapshot_sku_code__iexact=sku)
    )

    placement_base_qs = WarehouseEvent.objects.filter(
        agency=agency,
        event_type="placement_completed",
    ).filter(product_sku_match)
    processing_placement_match = Q(source_document_type="processing_placement") | Q(
        stock_context_type="processing"
    )
    processing_placement_base_qs = placement_base_qs.filter(processing_placement_match)
    income_base_qs = placement_base_qs.exclude(processing_placement_match)
    income_qs = _product_balance_event_period(income_base_qs, date_from, date_to)
    processing_placement_qs = _product_balance_event_period(
        processing_placement_base_qs,
        date_from,
        date_to,
    )

    restored_base_qs = WarehouseEvent.objects.filter(
        agency=agency,
        event_type__in={"manual_stock_restore", "manual_stock_released"},
    ).filter(product_sku_match)
    restored_qs = _product_balance_event_period(restored_base_qs, date_from, date_to)

    adjustment_incoming_base_qs = WarehouseEvent.objects.filter(
        agency=agency,
        event_type="inventory_balance_increased",
    ).filter(product_sku_match)
    adjustment_incoming_qs = _product_balance_event_period(adjustment_incoming_base_qs, date_from, date_to)

    adjustment_outgoing_base_qs = WarehouseEvent.objects.filter(
        agency=agency,
        event_type="inventory_balance_decreased",
    ).filter(product_sku_match)
    adjustment_outgoing_qs = _product_balance_event_period(adjustment_outgoing_base_qs, date_from, date_to)

    processing_match = product_sku_match
    if snapshot_ids:
        processing_match |= Q(payload__snapshot_id__in=snapshot_ids)
    if last_event_ids:
        processing_match |= Q(id__in=last_event_ids)
    processing_base_qs = WarehouseEvent.objects.filter(
        agency=agency,
        event_type="processing_consumed",
    ).filter(processing_match)
    processing_qs = _product_balance_event_period(processing_base_qs, date_from, date_to)

    shipped_match = Q(reserve__sku_code__iexact=sku) | product_sku_match
    if last_event_ids:
        shipped_match |= Q(id__in=last_event_ids)
    shipped_base_qs = WarehouseEvent.objects.filter(
        agency=agency,
        event_type="shipped",
    ).filter(shipped_match)
    shipped_qs = _product_balance_event_period(shipped_base_qs, date_from, date_to)

    manual_writeoff_match = (
        Q(payload__sku_code__iexact=sku)
        | Q(payload__sku__iexact=sku)
        | Q(payload__snapshot_sku_code__iexact=sku)
        | Q(payload__previous__sku_code__iexact=sku)
        | Q(payload__previous__sku__iexact=sku)
    )
    if snapshot_ids:
        manual_writeoff_match |= Q(payload__snapshot_id__in=snapshot_ids)
        manual_writeoff_match |= Q(payload__previous__snapshot_id__in=snapshot_ids)
    if last_event_ids:
        manual_writeoff_match |= Q(id__in=last_event_ids)
    manual_writeoff_base_qs = WarehouseEvent.objects.filter(
        agency=agency,
        event_type="manual_writeoff",
    ).filter(manual_writeoff_match)
    manual_writeoff_qs = _product_balance_event_period(manual_writeoff_base_qs, date_from, date_to)

    fbs_pick_base_qs = FbsPickScanEvent.objects.filter(
        allocation__balance__agency=agency,
        allocation__balance__sku_code__iexact=sku,
        stage=FbsPickScanEvent.STAGE_PICK_ITEM,
        result=FbsPickScanEvent.RESULT_SUCCESS,
    )
    fbs_pick_qs = _product_balance_timestamp_period(
        fbs_pick_base_qs,
        "created_at",
        date_from,
        date_to,
    )

    fbs_adjustment_base_qs = WarehouseEvent.objects.none()
    if fbs_balance_ids:
        fbs_adjustment_base_qs = WarehouseEvent.objects.filter(
            agency=agency,
            event_type="fbs_inventory_adjusted",
            payload__balance_id__in=fbs_balance_ids,
        )
    fbs_adjustment_qs = _product_balance_event_period(
        fbs_adjustment_base_qs,
        date_from,
        date_to,
    )

    incoming_quantity = int(income_qs.aggregate(total=Sum("qty"))["total"] or 0)
    processing_placement_quantity = int(
        processing_placement_qs.aggregate(total=Sum("qty"))["total"] or 0
    )
    restored_quantity = int(restored_qs.aggregate(total=Sum("qty"))["total"] or 0)
    adjustment_incoming_quantity = int(
        adjustment_incoming_qs.aggregate(total=Sum("qty"))["total"] or 0
    )
    adjustment_outgoing_quantity = int(
        adjustment_outgoing_qs.aggregate(total=Sum("qty"))["total"] or 0
    )
    shipped_quantity = int(shipped_qs.aggregate(total=Sum("qty"))["total"] or 0)
    processing_quantity = int(processing_qs.aggregate(total=Sum("qty"))["total"] or 0)
    manual_writeoff_quantity = int(manual_writeoff_qs.aggregate(total=Sum("qty"))["total"] or 0)
    fbs_picked_quantity = int(fbs_pick_qs.count())

    event_select = (
        "container",
        "container__parent_container",
        "reserve",
        "reserve__sku_ref",
        "from_location",
        "to_location",
        "performed_by",
    )
    income_events = list(income_qs.select_related(*event_select).order_by("-occurred_at", "-id")[:5000])
    processing_placement_events = list(
        processing_placement_qs.select_related(*event_select).order_by("-occurred_at", "-id")[:5000]
    )
    restored_events = list(restored_qs.select_related(*event_select).order_by("-occurred_at", "-id")[:5000])
    adjustment_incoming_events = list(
        adjustment_incoming_qs.select_related(*event_select).order_by("-occurred_at", "-id")[:5000]
    )
    adjustment_outgoing_events = list(
        adjustment_outgoing_qs.select_related(*event_select).order_by("-occurred_at", "-id")[:5000]
    )
    processing_events = list(processing_qs.select_related(*event_select).order_by("-occurred_at", "-id")[:5000])
    shipped_events = list(shipped_qs.select_related(*event_select).order_by("-occurred_at", "-id")[:5000])
    manual_writeoff_events = list(
        manual_writeoff_qs.select_related(*event_select).order_by("-occurred_at", "-id")[:5000]
    )
    fbs_adjustment_events = list(
        fbs_adjustment_qs.select_related(*event_select).order_by("-occurred_at", "-id")[:5000]
    )
    fbs_pick_events = list(
        fbs_pick_qs.select_related(
            "allocation__balance__box__pallet__cell__location",
            "allocation__order_item__order",
            "created_by",
        ).order_by("-created_at", "-id")[:5000]
    )

    fbs_adjustment_incoming_events = [
        event for event in fbs_adjustment_events if _fbs_inventory_adjustment_delta(event) > 0
    ]
    fbs_adjustment_outgoing_events = [
        event for event in fbs_adjustment_events if _fbs_inventory_adjustment_delta(event) < 0
    ]
    adjustment_incoming_quantity += sum(
        _fbs_inventory_adjustment_delta(event) for event in fbs_adjustment_incoming_events
    )
    adjustment_outgoing_quantity += sum(
        abs(_fbs_inventory_adjustment_delta(event)) for event in fbs_adjustment_outgoing_events
    )

    product_name = next((str(row.get("product_name") or "").strip() for row in stock_rows if row.get("product_name")), "")
    if not product_name:
        all_events = (
            income_events
            + processing_placement_events
            + restored_events
            + adjustment_incoming_events
            + processing_events
            + shipped_events
            + manual_writeoff_events
            + adjustment_outgoing_events
        )
        product_name = next((_product_balance_event_name(event) for event in all_events if _product_balance_event_name(event)), sku)

    movement_rows = [
        _product_balance_event_row(event, sku=sku, product_name=product_name, incoming=True, operation_type="Приход")
        for event in income_events
    ]
    movement_rows.extend(
        _product_balance_event_row(
            event,
            sku=sku,
            product_name=product_name,
            incoming=None,
            operation_type="Размещение после обработки (внутреннее)",
        )
        for event in processing_placement_events
    )
    movement_rows.extend(
        _product_balance_event_row(
            event,
            sku=sku,
            product_name=product_name,
            incoming=True,
            operation_type="Восстановление остатка",
        )
        for event in restored_events
    )
    movement_rows.extend(
        _product_balance_event_row(
            event,
            sku=sku,
            product_name=product_name,
            incoming=True,
            operation_type="Корректировка учёта: приход",
        )
        for event in adjustment_incoming_events
    )
    movement_rows.extend(
        _product_balance_event_row(
            event,
            sku=sku,
            product_name=product_name,
            incoming=True,
            operation_type="Корректировка FBS: приход",
        )
        for event in fbs_adjustment_incoming_events
    )
    movement_rows.extend(
        _product_balance_event_row(event, sku=sku, product_name=product_name, incoming=False, operation_type="Отгрузка")
        for event in shipped_events
    )
    movement_rows.extend(
        _product_balance_fbs_pick_row(event, sku=sku, product_name=product_name)
        for event in fbs_pick_events
    )
    movement_rows.extend(
        _product_balance_event_row(
            event,
            sku=sku,
            product_name=product_name,
            incoming=False,
            operation_type="Списание в обработке",
        )
        for event in processing_events
    )
    movement_rows.extend(
        _product_balance_event_row(
            event,
            sku=sku,
            product_name=product_name,
            incoming=False,
            operation_type="Ручное списание",
        )
        for event in manual_writeoff_events
    )
    movement_rows.extend(
        _product_balance_event_row(
            event,
            sku=sku,
            product_name=product_name,
            incoming=False,
            operation_type="Корректировка учёта: расход",
        )
        for event in adjustment_outgoing_events
    )
    movement_rows.extend(
        _product_balance_event_row(
            event,
            sku=sku,
            product_name=product_name,
            incoming=False,
            operation_type="Корректировка FBS: расход",
        )
        for event in fbs_adjustment_outgoing_events
    )

    active_reserves = list(
        WarehouseReserve.objects.filter(
            agency=agency,
            sku_code__iexact=sku,
            status__in=RESERVE_ACTIVE_STATUSES,
        )
        .select_related("sku_ref")
        .order_by("-created_at", "-id")[:5000]
    )
    reserve_rows = [
        {
            "reserve_number": reserve.id,
            "created_at": reserve.created_at,
            "reserve_type": reserve.get_reserve_type_display(),
            "document_number": reserve.context_id,
            "reserved_quantity": int(reserve.qty_reserved or 0),
            "satisfied_quantity": int(reserve.qty_satisfied or 0),
            "reserve_balance": max(int(reserve.qty_reserved or 0) - int(reserve.qty_satisfied or 0), 0),
            "status": reserve.get_status_display(),
        }
        for reserve in active_reserves
    ]

    incoming_total_quantity = incoming_quantity + restored_quantity + adjustment_incoming_quantity
    outgoing_quantity = (
        shipped_quantity
        + fbs_picked_quantity
        + processing_quantity
        + manual_writeoff_quantity
        + adjustment_outgoing_quantity
    )
    warehouse_stock_rows = [row for row in stock_rows if row.get("stock_area") == "Общий склад"]
    fbs_stock_rows = [row for row in stock_rows if row.get("stock_area") == "FBS"]
    warehouse_physical_quantity = sum(int(row["quantity"] or 0) for row in warehouse_stock_rows)
    fbs_physical_quantity = sum(int(row["quantity"] or 0) for row in fbs_stock_rows)
    physical_quantity = warehouse_physical_quantity + fbs_physical_quantity
    reserved_quantity = sum(
        int(row["processing_reserved_quantity"] or 0)
        + int(row["shipping_reserved_quantity"] or 0)
        + int(row["other_reserved_quantity"] or 0)
        for row in stock_rows
    )
    available_quantity = sum(int(row["available_quantity"] or 0) for row in stock_rows)
    period_limited = bool(date_from or date_to)
    future_incoming_quantity = 0
    future_outgoing_quantity = 0
    if date_to:
        future_incoming_quantity = sum(
            int(queryset.filter(occurred_at__date__gt=date_to).aggregate(total=Sum("qty"))["total"] or 0)
            for queryset in (
                income_base_qs,
                restored_base_qs,
                adjustment_incoming_base_qs,
            )
        )
        future_outgoing_quantity = sum(
            int(queryset.filter(occurred_at__date__gt=date_to).aggregate(total=Sum("qty"))["total"] or 0)
            for queryset in (
                processing_base_qs,
                shipped_base_qs,
                manual_writeoff_base_qs,
                adjustment_outgoing_base_qs,
            )
        )
        future_outgoing_quantity += int(
            fbs_pick_base_qs.filter(created_at__date__gt=date_to).count()
        )
        for event in fbs_adjustment_base_qs.filter(occurred_at__date__gt=date_to):
            delta = _fbs_inventory_adjustment_delta(event)
            if delta > 0:
                future_incoming_quantity += delta
            elif delta < 0:
                future_outgoing_quantity += abs(delta)

    period_end_quantity = physical_quantity - future_incoming_quantity + future_outgoing_quantity
    period_start_quantity = period_end_quantity - incoming_total_quantity + outgoing_quantity
    control_balance = (
        period_start_quantity
        + incoming_total_quantity
        - outgoing_quantity
        - period_end_quantity
    )
    balance_status = (
        "Баланс сходится"
        if period_start_quantity >= 0 and period_end_quantity >= 0 and control_balance == 0
        else "Требуется сверка: расчётный остаток получился отрицательным"
    )
    warehouse_zone_quantities = {
        zone: sum(
            int(row.get("quantity") or 0)
            for row in warehouse_stock_rows
            if str(row.get("zone_code") or "").upper() == zone
        )
        for zone in ("OS", "OTG", "OBR")
    }
    summary = {
        "client_id": agency.id,
        "client_name": _agency_name(agency),
        "sku": sku,
        "product_name": product_name,
        "period_limited": period_limited,
        "period_label": _product_balance_period_label(date_from, date_to),
        "incoming_quantity": incoming_quantity,
        "processing_placement_quantity": processing_placement_quantity,
        "restored_quantity": restored_quantity,
        "adjustment_incoming_quantity": adjustment_incoming_quantity,
        "incoming_total_quantity": incoming_total_quantity,
        "shipped_quantity": shipped_quantity,
        "fbs_picked_quantity": fbs_picked_quantity,
        "processing_consumed_quantity": processing_quantity,
        "manual_writeoff_quantity": manual_writeoff_quantity,
        "adjustment_outgoing_quantity": adjustment_outgoing_quantity,
        "outgoing_quantity": outgoing_quantity,
        "physical_quantity": physical_quantity,
        "warehouse_physical_quantity": warehouse_physical_quantity,
        "fbs_physical_quantity": fbs_physical_quantity,
        "warehouse_os_quantity": warehouse_zone_quantities["OS"],
        "warehouse_otg_quantity": warehouse_zone_quantities["OTG"],
        "warehouse_obr_quantity": warehouse_zone_quantities["OBR"],
        "reserved_quantity": reserved_quantity,
        "available_quantity": available_quantity,
        "period_start_quantity": period_start_quantity,
        "period_end_quantity": period_end_quantity,
        "control_balance": control_balance,
        "balance_status": balance_status,
    }
    return movement_rows, "", {"summary": summary, "stock_rows": stock_rows, "reserve_rows": reserve_rows}


def _product_balance_event_period(qs, date_from, date_to):
    if date_from:
        qs = qs.filter(occurred_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(occurred_at__date__lte=date_to)
    return qs


def _product_balance_timestamp_period(qs, field_name: str, date_from, date_to):
    if date_from:
        qs = qs.filter(**{f"{field_name}__date__gte": date_from})
    if date_to:
        qs = qs.filter(**{f"{field_name}__date__lte": date_to})
    return qs


def _fbs_inventory_adjustment_delta(event: WarehouseEvent) -> int:
    payload = event.payload if isinstance(event.payload, dict) else {}
    try:
        return int(payload.get("delta") or 0)
    except (TypeError, ValueError):
        return 0


def _product_balance_event_name(event: WarehouseEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    reserve_sku = getattr(getattr(event, "reserve", None), "sku_ref", None)
    return str(
        payload.get("name")
        or payload.get("product_name")
        or getattr(reserve_sku, "name", "")
        or ""
    ).strip()


def _product_balance_event_row(
    event: WarehouseEvent,
    *,
    sku: str,
    product_name: str,
    incoming: bool | None,
    operation_type: str,
) -> dict:
    qty = int(event.qty or 0)
    container = event.container
    parent = getattr(container, "parent_container", None)
    box_code = ""
    pallet_code = ""
    if container is not None:
        if str(container.container_type or "").strip() == "box":
            box_code = str(container.container_code or "").strip()
            pallet_code = str(getattr(parent, "container_code", "") or "").strip()
        else:
            pallet_code = str(container.container_code or "").strip()
    return {
        "operation_at": event.occurred_at,
        "operation_type": operation_type,
        "document_number": event.source_document_id or event.stock_context_id or "",
        "product_name": _product_balance_event_name(event) or product_name or sku,
        "article": sku,
        "incoming_quantity": qty if incoming is True else 0,
        "outgoing_quantity": qty if incoming is False else 0,
        "pallet_code": pallet_code,
        "box_code": box_code,
        "location": _cell_label(event.to_location or event.from_location) or event.to_zone_code or event.from_zone_code,
        "responsible": _user_label(event.performed_by),
    }


def _product_balance_fbs_pick_row(event: FbsPickScanEvent, *, sku: str, product_name: str) -> dict:
    allocation = event.allocation
    balance = allocation.balance
    box = balance.box
    pallet = box.pallet
    order_item = allocation.order_item
    return {
        "operation_at": event.created_at,
        "operation_type": "FBS: отбор в заказ",
        "document_number": order_item.order.external_order_id,
        "product_name": order_item.product_name or balance.name or product_name or sku,
        "article": sku,
        "incoming_quantity": 0,
        "outgoing_quantity": 1,
        "pallet_code": pallet.pallet_code,
        "box_code": box.box_code,
        "location": _cell_label(pallet.cell.location) or pallet.cell.cell_code,
        "responsible": _user_label(event.created_by),
    }


def _product_balance_period_label(date_from, date_to) -> str:
    if date_from and date_to:
        return f"{date_from:%d.%m.%Y}–{date_to:%d.%m.%Y}"
    if date_from:
        return f"с {date_from:%d.%m.%Y}"
    if date_to:
        return f"по {date_to:%d.%m.%Y}"
    return "За всё время"


def _stock_queryset(params):
    qs = (
        WarehouseStockSnapshot.objects.filter(is_archived=False)
        .select_related("agency", "sku_ref", "location", "container", "parent_container", "last_event")
        .order_by("agency__agn_name", "sku_code", "location__warehouse_code", "zone_code", "location__row_no", "id")
    )
    qs = _apply_common_stock_filters(qs, params)
    stock_type = str(params.get("stock_type") or "").strip()
    if stock_type == "available":
        qs = qs.filter(available_qty__gt=0)
    elif stock_type == "reserved":
        qs = qs.filter(Q(processing_reserved_qty__gt=0) | Q(shipping_reserved_qty__gt=0) | Q(other_reserved_qty__gt=0))
    elif stock_type == "blocked":
        qs = qs.filter(other_reserved_qty__gt=0)
    if _truthy(params.get("has_reserve")):
        qs = qs.filter(Q(processing_reserved_qty__gt=0) | Q(shipping_reserved_qty__gt=0) | Q(other_reserved_qty__gt=0))
    if _truthy(params.get("only_positive")):
        qs = qs.filter(qty__gt=0)
    if _truthy(params.get("only_available")):
        qs = qs.filter(available_qty__gt=0)
    if _truthy(params.get("only_zero")):
        qs = qs.filter(qty=0)
    elif not params.get("only_zero"):
        qs = qs.filter(qty__gt=0)
    return qs


def _apply_common_stock_filters(qs, params):
    if params.get("client_id"):
        qs = qs.filter(agency_id=_int_or_none(params.get("client_id")) or 0)
    if params.get("warehouse_id"):
        warehouse = str(params.get("warehouse_id") or "").strip()
        qs = qs.filter(Q(location__warehouse_code__icontains=warehouse) | Q(location__display_name__icontains=warehouse))
    if params.get("zone"):
        qs = qs.filter(zone_code__icontains=str(params.get("zone")).strip())
    if params.get("cell"):
        cell = str(params.get("cell") or "").strip()
        cell_q = Q(location__location_code__icontains=cell) | Q(location__display_name__icontains=cell)
        if cell.isdigit():
            cell_q |= Q(location__cell_no=int(cell))
        qs = qs.filter(cell_q)
    if params.get("product"):
        text = str(params.get("product") or "").strip()
        qs = qs.filter(Q(name__icontains=text) | Q(sku_code__icontains=text) | Q(sku_ref__name__icontains=text))
    if params.get("category"):
        text = str(params.get("category") or "").strip()
        qs = qs.filter(Q(goods_type__icontains=text) | Q(sku_ref__tovar_category__icontains=text) | Q(sku_ref__type_tovar__icontains=text))
    if params.get("sku") or params.get("article"):
        text = str(params.get("sku") or params.get("article") or "").strip()
        qs = qs.filter(Q(sku_code__icontains=text) | Q(sku_ref__sku_code__icontains=text))
    if params.get("barcode"):
        qs = qs.filter(barcode__icontains=str(params.get("barcode") or "").strip())
    if params.get("batch"):
        qs = qs.filter(source_context_id__icontains=str(params.get("batch") or "").strip())
    if params.get("status"):
        qs = qs.filter(warehouse_state_code__icontains=str(params.get("status") or "").strip())
    date_from, date_to = _date_range(params)
    if date_from:
        qs = qs.filter(updated_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(updated_at__date__lte=date_to)
    return qs


def _stock_rows(params) -> list[dict]:
    return [_stock_row(snapshot) for snapshot in _stock_queryset(params)[:5000]]


def _stock_row(snapshot: WarehouseStockSnapshot) -> dict:
    location = snapshot.location
    reserved = int(snapshot.processing_reserved_qty or 0) + int(snapshot.shipping_reserved_qty or 0) + int(snapshot.other_reserved_qty or 0)
    warehouse = getattr(location, "warehouse_code", "") or "MSK"
    cell = _cell_label(location)
    last_movement = getattr(snapshot.last_event, "occurred_at", None) or snapshot.updated_at
    sku = snapshot.sku_ref
    return {
        "article": snapshot.sku_code or getattr(sku, "sku_code", ""),
        "sku": getattr(sku, "sku_code", "") or snapshot.sku_code,
        "barcode": snapshot.barcode,
        "product_name": snapshot.name or getattr(sku, "name", "") or snapshot.sku_code,
        "client_name": _agency_name(snapshot.agency),
        "warehouse_name": warehouse,
        "zone": snapshot.zone_code,
        "cell": cell,
        "batch": snapshot.source_context_id,
        "actual_quantity": int(snapshot.qty or 0),
        "available_quantity": int(snapshot.available_qty or 0),
        "reserved_quantity": reserved,
        "blocked_quantity": int(snapshot.other_reserved_qty or 0),
        "unit": "шт",
        "last_movement_at": last_movement,
        "status": snapshot.warehouse_state_code,
        "category": getattr(sku, "tovar_category", "") or snapshot.goods_type,
        "row": getattr(location, "row_no", 0) if location else 0,
        "section": getattr(location, "section_no", 0) if location else 0,
        "tier": getattr(location, "tier_no", 0) if location else 0,
        "location_cell": getattr(location, "cell_no", 0) if location else 0,
    }


def _box_flow_queryset(params):
    qs = (
        WarehouseStockSnapshot.objects.filter(qty__gt=0)
        .select_related("agency", "sku_ref", "location", "container", "parent_container", "active_operation", "last_event")
        .order_by("agency__agn_name", "sku_code", "source_context_id", "container_code", "id")
    )
    return _apply_common_stock_filters(qs, params)


def _box_flow_rows(params) -> list[dict]:
    snapshots = list(_box_flow_queryset(params)[:5000])
    income_events, outcome_events = _box_flow_event_maps(snapshots)
    rows: list[dict] = []
    for index, snapshot in enumerate(snapshots, start=1):
        row = normalize_stock_row_from_snapshot(snapshot) or {}
        container = snapshot.container if getattr(snapshot.container, "container_type", "") == "box" else None
        income = _box_flow_pick_event(snapshot, row, income_events, BOX_FLOW_INCOME_EVENTS, latest=False)
        outcome = _box_flow_pick_event(snapshot, row, outcome_events, BOX_FLOW_OUTCOME_EVENTS, latest=True)
        rows.append(
            {
                "number": index,
                "client_name": _agency_name(snapshot.agency),
                "pallet_code": row.get("pallet_code") or "",
                "box_code": row.get("box_code") or row.get("pallet_code") or snapshot.container_code or "",
                "sku": row.get("sku") or snapshot.sku_code,
                "product_name": row.get("name") or snapshot.name or snapshot.sku_code,
                "barcode": row.get("barcode") or snapshot.barcode,
                "quantity": int(snapshot.qty or 0),
                "status": _box_flow_status(snapshot),
                "width_mm": int(getattr(container, "width_mm", 0) or 0) or None,
                "height_mm": int(getattr(container, "height_mm", 0) or 0) or None,
                "depth_mm": int(getattr(container, "depth_mm", 0) or 0) or None,
                "volume_m3": _box_flow_volume(container),
                "weight_g": int(getattr(container, "gross_weight_g", 0) or 0) or None,
                "income_at": getattr(income, "occurred_at", None) or snapshot.created_at,
                "income_document": _box_flow_income_document(snapshot, income),
                "outcome_at": getattr(outcome, "occurred_at", None),
                "outcome_document": _box_flow_outcome_document(snapshot, outcome),
            }
        )
    return rows


def _box_flow_event_maps(snapshots: list[WarehouseStockSnapshot]) -> tuple[dict[str, list[WarehouseEvent]], dict[str, list[WarehouseEvent]]]:
    container_ids = {int(snapshot.container_id or 0) for snapshot in snapshots if snapshot.container_id}
    documents = {
        str(value or "").strip()
        for snapshot in snapshots
        for value in (
            snapshot.source_context_id,
            getattr(snapshot.last_event, "stock_context_id", ""),
            getattr(snapshot.last_event, "source_document_id", ""),
        )
        if str(value or "").strip()
    }
    if not container_ids and not documents:
        return {}, {}
    query = Q()
    if container_ids:
        query |= Q(container_id__in=container_ids)
    if documents:
        query |= Q(source_document_id__in=documents) | Q(stock_context_id__in=documents)
    events = list(
        WarehouseEvent.objects.filter(query)
        .select_related("container")
        .order_by("occurred_at", "id")[:20000]
    )
    income: dict[str, list[WarehouseEvent]] = {}
    outcome: dict[str, list[WarehouseEvent]] = {}
    for event in events:
        targets = _box_flow_event_keys(event)
        target_map = income if event.event_type in BOX_FLOW_INCOME_EVENTS else outcome if event.event_type in BOX_FLOW_OUTCOME_EVENTS else None
        if target_map is None:
            continue
        for key in targets:
            target_map.setdefault(key, []).append(event)
    return income, outcome


def _box_flow_event_keys(event: WarehouseEvent) -> set[str]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    keys: set[str] = set()
    if event.container_id:
        keys.add(f"container:{int(event.container_id)}")
    box_code = str(payload.get("box_code") or payload.get("source_box_code") or getattr(event.container, "container_code", "") or "").strip()
    if box_code:
        keys.add(f"box:{box_code.lower()}")
    for value in (event.source_document_id, event.stock_context_id):
        text = str(value or "").strip()
        if text:
            keys.add(f"document:{text.lower()}")
    return keys


def _box_flow_snapshot_keys(snapshot: WarehouseStockSnapshot, row: dict) -> set[str]:
    keys: set[str] = set()
    if snapshot.container_id:
        keys.add(f"container:{int(snapshot.container_id)}")
    for value in (row.get("box_code"), snapshot.container_code, getattr(snapshot.container, "container_code", "")):
        text = str(value or "").strip()
        if text:
            keys.add(f"box:{text.lower()}")
    for value in (
        snapshot.source_context_id,
        getattr(snapshot.last_event, "stock_context_id", ""),
        getattr(snapshot.last_event, "source_document_id", ""),
    ):
        text = str(value or "").strip()
        if text:
            keys.add(f"document:{text.lower()}")
    return keys


def _box_flow_pick_event(
    snapshot: WarehouseStockSnapshot,
    row: dict,
    events_by_key: dict[str, list[WarehouseEvent]],
    allowed_types: set[str],
    *,
    latest: bool,
) -> WarehouseEvent | None:
    candidates: list[WarehouseEvent] = []
    last_event = snapshot.last_event
    if last_event is not None and last_event.event_type in allowed_types:
        candidates.append(last_event)
    seen: set[int] = {int(getattr(event, "id", 0) or 0) for event in candidates}
    for key in _box_flow_snapshot_keys(snapshot, row):
        for event in events_by_key.get(key, []):
            event_id = int(event.id or 0)
            if event_id in seen or event.event_type not in allowed_types:
                continue
            candidates.append(event)
            seen.add(event_id)
    if not candidates:
        return None
    return sorted(candidates, key=lambda event: (event.occurred_at, event.id), reverse=latest)[0]


def _box_flow_volume(container) -> float | None:
    width = int(getattr(container, "width_mm", 0) or 0)
    height = int(getattr(container, "height_mm", 0) or 0)
    depth = int(getattr(container, "depth_mm", 0) or 0)
    if not (width and height and depth):
        return None
    return round((width * height * depth) / 1_000_000_000, 6)


def _box_flow_income_document(snapshot: WarehouseStockSnapshot, event: WarehouseEvent | None) -> str:
    return (
        str(getattr(event, "source_document_id", "") or getattr(event, "stock_context_id", "") or "").strip()
        or str(snapshot.source_context_id or "").strip()
    )


def _box_flow_outcome_document(snapshot: WarehouseStockSnapshot, event: WarehouseEvent | None) -> str:
    if event is None:
        return ""
    return str(event.source_document_id or event.stock_context_id or getattr(snapshot.last_event, "stock_context_id", "") or "").strip()


def _box_flow_status(snapshot: WarehouseStockSnapshot) -> str:
    last_event_type = str(getattr(snapshot.last_event, "event_type", "") or "").strip()
    state = str(snapshot.warehouse_state_code or "").strip()
    if last_event_type == "shipped" or state == "shipped":
        return "Отгружен"
    labels = {
        "stored": "На складе",
        "received_unplaced": "Принят, не размещён",
        "placed_in_receiving": "В зоне приёмки",
        "placed_in_storage": "На складе",
        "in_processing_zone": "В обработке",
        "placed_after_processing": "После обработки",
        "shipping_reserved": "Зарезервирован",
        "in_otg": "В зоне отгрузки",
        "palletizing": "Паллетизация",
        "ready_for_loading": "Готов к погрузке",
        "assigned_to_trip": "Назначен в рейс",
        "loading_in_progress": "Погрузка",
        "loaded_to_vehicle": "Погружен",
        "partially_shipped": "Частично отгружен",
    }
    return labels.get(state, labels.get(last_event_type, state or last_event_type or "На складе"))


def _event_label(event_type: str) -> str:
    """Человеческое имя события. Неизвестный тип показываем как есть."""
    value = str(event_type or "").strip()
    return EVENT_LABELS.get(value, value)


def _movement_direction(event_type: str) -> str:
    """Направление строки. Неизвестное событие считаем служебным, не падаем."""
    value = str(event_type or "").strip()
    if value in MOVEMENT_INCOME_EVENTS:
        return "income"
    if value in MOVEMENT_OUTCOME_EVENTS:
        return "outcome"
    if value in MOVEMENT_RESERVE_EVENTS or value.endswith(("_reserved", "_reserve_released")):
        return "reserve"
    if value in MOVEMENT_TRANSFER_EVENTS:
        return "transfer"
    return "service"


def _document_parts(event) -> tuple[str, str]:
    """Тип и номер документа-основания.

    На production 84 % событий приходят с пустым ``source_document_type``,
    зато ``stock_context_*`` заполнен почти всегда. Поэтому документ ищем
    по цепочке: документ -> складской контекст -> складская операция.
    Номер при этом совпадает с прежним поведением отчёта.
    """
    doc_type = str(getattr(event, "source_document_type", "") or "").strip()
    doc_id = str(getattr(event, "source_document_id", "") or "").strip()
    ctx_type = str(getattr(event, "stock_context_type", "") or "").strip()
    ctx_id = str(getattr(event, "stock_context_id", "") or "").strip()
    if doc_type and doc_id:
        return SOURCE_DOCUMENT_LABELS.get(doc_type, doc_type), doc_id
    if ctx_type and ctx_id:
        return f"{STOCK_CONTEXT_LABELS.get(ctx_type, ctx_type)} · контекст", ctx_id
    if doc_id:
        return (SOURCE_DOCUMENT_LABELS.get(doc_type, doc_type) if doc_type else ""), doc_id
    if ctx_id:
        return (STOCK_CONTEXT_LABELS.get(ctx_type, ctx_type) if ctx_type else ""), ctx_id
    context_id = str(getattr(getattr(event, "operation", None), "context_id", "") or "").strip()
    if context_id:
        return "Складская операция", context_id
    return "", ""


def _container_codes(event) -> tuple[str, str]:
    """Код короба и код родительской паллеты события."""
    container = getattr(event, "container", None)
    if container is None:
        return "", ""
    own = str(getattr(container, "container_code", "") or "").strip()
    parent = getattr(container, "parent_container", None)
    parent_code = str(getattr(parent, "container_code", "") or "").strip()
    if str(getattr(container, "container_type", "") or "") in {"pallet", "mixed_pallet"}:
        return "", own or parent_code
    return own, parent_code


def _movement_queryset(
    params,
    *,
    event_types: Iterable[str] | None = None,
    visible_agencies=None,
    exact_product: bool = False,
    product_value: str = "",
):
    qs = (
        WarehouseEvent.objects.select_related(
            "agency",
            "operation",
            "operation__requested_by",
            "operation_task",
            "operation_task__assigned_to",
            "from_location",
            "to_location",
            "performed_by",
            "container",
            "container__parent_container",
            "reserve",
            "reserve__sku_ref",
        )
        .order_by("-occurred_at", "-id")
    )
    if visible_agencies is not None:
        qs = qs.filter(agency__in=visible_agencies)
    if event_types:
        qs = qs.filter(event_type__in=list(event_types))
    if params.get("client_id"):
        qs = qs.filter(agency_id=_int_or_none(params.get("client_id")) or 0)
    if params.get("operation_type"):
        qs = qs.filter(Q(event_type__icontains=params.get("operation_type")) | Q(operation__operation_type__icontains=params.get("operation_type")))
    if params.get("warehouse_id"):
        warehouse = str(params.get("warehouse_id") or "").strip()
        qs = qs.filter(Q(from_location__warehouse_code__icontains=warehouse) | Q(to_location__warehouse_code__icontains=warehouse))
    if params.get("zone"):
        zone = str(params.get("zone") or "").strip()
        qs = qs.filter(Q(from_zone_code__icontains=zone) | Q(to_zone_code__icontains=zone))
    if params.get("cell"):
        cell = str(params.get("cell") or "").strip()
        cell_q = Q(from_location__display_name__icontains=cell) | Q(to_location__display_name__icontains=cell)
        if cell.isdigit():
            cell_q |= Q(from_location__cell_no=int(cell)) | Q(to_location__cell_no=int(cell))
        qs = qs.filter(cell_q)
    if product_value or params.get("sku") or params.get("article") or params.get("product") or params.get("barcode"):
        text = str(
            product_value
            or params.get("sku")
            or params.get("article")
            or params.get("product")
            or params.get("barcode")
            or ""
        ).strip()
        # Товар в событии не хранится, поэтому ищем через складские снимки и резерв.
        # Скан payload убран: он почти ничего не находил и шёл без индекса.
        if exact_product:
            product_match = Q(sku_code__iexact=text) | Q(barcode__iexact=text) | Q(name__iexact=text)
            reserve_match = Q(reserve__sku_code__iexact=text) | Q(reserve__barcode__iexact=text)
        else:
            product_match = Q(sku_code__icontains=text) | Q(barcode__icontains=text) | Q(name__icontains=text)
            reserve_match = Q(reserve__sku_code__icontains=text) | Q(reserve__barcode__icontains=text)
        matched = WarehouseStockSnapshot.objects.filter(product_match).exclude(container_id=None)
        if visible_agencies is not None:
            matched = matched.filter(agency__in=visible_agencies)
        matched = matched.values_list("container_id", flat=True).distinct()
        container_ids = list(matched[: MOVEMENT_CONTAINER_LIMIT + 1])
        if len(container_ids) > MOVEMENT_CONTAINER_LIMIT:
            raise MovementSelectionTooBroad(len(container_ids))
        condition = reserve_match
        condition |= Q(source_document_id__iexact=text) if exact_product else Q(source_document_id__icontains=text)
        if container_ids:
            condition |= Q(container_id__in=container_ids)
        qs = qs.filter(condition)
    if params.get("batch"):
        qs = qs.filter(stock_context_id__icontains=str(params.get("batch") or "").strip())
    if params.get("responsible"):
        text = str(params.get("responsible") or "").strip()
        qs = qs.filter(Q(performed_by__first_name__icontains=text) | Q(performed_by__last_name__icontains=text) | Q(performed_by__username__icontains=text))
    date_from, date_to = _date_range(params)
    if date_from:
        qs = qs.filter(occurred_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(occurred_at__date__lte=date_to)
    return qs


def _resolve_container_products(events) -> dict[int, dict]:
    """Товар событий по составу их короба. Один запрос на всю выборку, без N+1."""
    container_ids = {event.container_id for event in events if event.container_id}
    if not container_ids:
        return {}
    grouped: dict[int, dict[str, dict]] = {}
    rows = WarehouseStockSnapshot.objects.filter(container_id__in=container_ids).values(
        "container_id", "sku_code", "name", "barcode"
    )
    for row in rows.iterator(chunk_size=2000):
        code = str(row["sku_code"] or "").strip()
        if not code:
            continue
        grouped.setdefault(row["container_id"], {}).setdefault(code, row)
    return {
        container_id: _product_from_snapshot_bucket(bucket)
        for container_id, bucket in grouped.items()
    }


def _product_from_snapshot_bucket(bucket: dict) -> dict:
    """Товар короба по его снимкам. Смешанный короб показываем составом."""
    if len(bucket) == 1:
        row = next(iter(bucket.values()))
        code = str(row.get("sku_code") or "").strip()
        return {
            "article": code,
            "product_name": str(row.get("name") or "").strip() or code,
            "barcode": str(row.get("barcode") or "").strip(),
        }
    return {"article": "", "product_name": f"{len(bucket)} SKU в коробе", "barcode": ""}


def _event_product(event, container_product: dict | None = None) -> dict:
    """Товар строки: payload -> резерв -> состав короба. Иначе прочерк.

    Порядок не случаен. В payload товар лежит только у ``placement_completed``,
    зато там он точный. Резерв называет конкретный SKU расходной операции и
    заполнен у ``shipped`` в 99,9 % и у ``shipping_reserved`` в 100 %. Состав
    короба — общий путь, но у смешанного короба однозначного ответа нет.
    """
    payload = event.payload if isinstance(event.payload, dict) else {}
    article = str(payload.get("sku_code") or payload.get("sku") or "").strip()
    name = str(payload.get("name") or payload.get("product_name") or "").strip()
    if article or name:
        return {
            "article": article,
            "product_name": name or article or "—",
            "barcode": str(payload.get("barcode") or "").strip(),
        }
    reserve = getattr(event, "reserve", None)
    reserve_article = str(getattr(reserve, "sku_code", "") or "").strip()
    if reserve_article:
        reserve_name = str(getattr(getattr(reserve, "sku_ref", None), "name", "") or "").strip()
        return {
            "article": reserve_article,
            "product_name": reserve_name or reserve_article,
            "barcode": str(getattr(reserve, "barcode", "") or "").strip(),
        }
    if container_product:
        return dict(container_product)
    return {"article": "", "product_name": "—", "barcode": ""}


def _movement_rows(
    params,
    *,
    event_types: Iterable[str] | None,
    visible_agencies=None,
    exact_product: bool = False,
    product_value: str = "",
) -> list[dict]:
    events = list(
        _movement_queryset(
            params,
            event_types=event_types,
            visible_agencies=visible_agencies,
            exact_product=exact_product,
            product_value=product_value,
        )[:5000]
    )
    products = _resolve_container_products(events)
    rows = [_movement_row(event, products.get(event.container_id)) for event in events]
    if exact_product and str(product_value or "").strip():
        rows = [
            row
            for row in rows
            if _movement_row_matches_exact_product(row, product_value)
        ]
    return rows


def _movement_row_matches_exact_product(row: dict, product_value: str) -> bool:
    """Не подмешивать другие SKU из общего короба в точный отчёт товара."""
    expected = str(product_value or "").strip().casefold()
    actual = str(row.get("article") or "").strip().casefold()
    return bool(expected and actual == expected)


def _movement_row(event: WarehouseEvent, container_product: dict | None = None) -> dict:
    payload = event.payload if isinstance(event.payload, dict) else {}
    qty = int(event.qty or payload.get("qty") or payload.get("quantity") or 0)
    operation = event.operation
    document_type, document_number = _document_parts(event)
    box_code, pallet_code = _container_codes(event)
    product = _event_product(event, container_product)
    return {
        "operation_at": event.occurred_at,
        # operation_type сохранён без изменений: его читают отчёты
        # «Приход товара», «Расход товара» и «Перемещения товара».
        "operation_type": _operation_label(event.event_type),
        "event_label": _event_label(event.event_type),
        "event_code": str(event.event_type or ""),
        "direction": DIRECTION_LABELS[_movement_direction(event.event_type)],
        "document_type": document_type,
        "document_number": document_number,
        "product_name": product["product_name"],
        "article": product["article"],
        "barcode": product["barcode"],
        "operation_quantity": qty,
        "warehouse_from": getattr(event.from_location, "warehouse_code", "") or "",
        "warehouse_to": getattr(event.to_location, "warehouse_code", "") or "",
        "cell_from": _cell_label(event.from_location) or event.from_zone_code,
        "cell_to": _cell_label(event.to_location) or event.to_zone_code,
        "pallet_code": pallet_code,
        "box_code": box_code,
        "user": _user_label(event.performed_by),
        "performed_by_role": str(getattr(event, "performed_by_role", "") or ""),
        "comment": payload.get("comment") or getattr(operation, "comment", "") or "",
        "status": getattr(operation, "status", "") or "",
        "initiator": _user_label(getattr(operation, "requested_by", None)),
        "executor": _user_label(getattr(event.operation_task, "assigned_to", None)) or getattr(event.operation_task, "assigned_to_name", ""),
        "duration": _duration_label(getattr(operation, "started_at", None), getattr(operation, "completed_at", None)),
    }


def _income_rows(params) -> list[dict]:
    event_rows = []
    for row in _movement_rows(params, event_types=INCOME_EVENTS):
        event_rows.append(
            {
                "income_at": row["operation_at"],
                "receiving_number": row["document_number"],
                "source": row["warehouse_from"] or row["operation_type"],
                "client_name": "",
                "product_name": row["product_name"],
                "article": row["article"],
                "accepted_quantity": row["operation_quantity"],
                "shortage_quantity": 0,
                "surplus_quantity": 0,
                "defect_quantity": 0,
                "warehouse_name": row["warehouse_to"] or row["warehouse_from"],
                "zone": row["cell_to"],
                "cell": row["cell_to"],
                "batch": row["document_number"],
                "expiration_at": None,
                "responsible": row["user"],
            }
        )
    if event_rows:
        return event_rows
    return [
        {
            "income_at": item["last_movement_at"],
            "receiving_number": item["batch"],
            "source": item["batch"],
            "client_name": item["client_name"],
            "product_name": item["product_name"],
            "article": item["article"],
            "accepted_quantity": item["actual_quantity"],
            "shortage_quantity": 0,
            "surplus_quantity": 0,
            "defect_quantity": 0,
            "warehouse_name": item["warehouse_name"],
            "zone": item["zone"],
            "cell": item["cell"],
            "batch": item["batch"],
            "expiration_at": None,
            "responsible": "",
        }
        for item in _stock_rows(params)
        if item["batch"]
    ]


def _outcome_rows(params) -> list[dict]:
    qs = (
        ShippingOrderItem.objects.select_related("order", "order__agency")
        .exclude(order__status=ShippingOrder.STATUS_CANCELED)
        .order_by("-order__shipped_at", "-order__created_at", "-id")
    )
    qs = _apply_shipping_filters(qs, params)
    rows = []
    for item in qs[:5000]:
        order = item.order
        rows.append(
            {
                "date": order.shipped_at or order.updated_at or order.created_at,
                "outcome_type": "Отгрузка" if item.qty_shipped else "Резерв / подбор",
                "document_number": order.number,
                "order_number": order.number,
                "client_name": _agency_name(order.agency),
                "product_name": item.name or item.sku_code,
                "article": item.sku_code,
                "quantity": int(item.qty_shipped or item.qty_reserved or item.qty_requested or 0),
                "warehouse_name": order.destination_warehouse or order.destination_address or "",
                "cell": "",
                "reason": order.get_status_display(),
                "recipient": order.destination_address or order.destination_warehouse or "",
                "responsible": _user_label(order.created_by),
            }
        )
    if rows:
        return rows
    return [
        {
            "date": row["operation_at"],
            "outcome_type": row["operation_type"],
            "document_number": row["document_number"],
            "order_number": row["document_number"],
            "client_name": "",
            "product_name": row["product_name"],
            "article": row["article"],
            "quantity": row["operation_quantity"],
            "warehouse_name": row["warehouse_from"],
            "cell": row["cell_from"],
            "reason": row["comment"],
            "recipient": row["warehouse_to"],
            "responsible": row["user"],
        }
        for row in _movement_rows(params, event_types=OUTCOME_EVENTS)
    ]


def _transfer_rows(params) -> list[dict]:
    rows = []
    for row in _movement_rows(params, event_types=TRANSFER_EVENTS):
        rows.append(
            {
                "date": row["operation_at"],
                "transfer_number": row["document_number"],
                "product_name": row["product_name"],
                "article": row["article"],
                "quantity": row["operation_quantity"],
                "warehouse_from": row["warehouse_from"],
                "warehouse_to": row["warehouse_to"],
                "zone_from": row["cell_from"].split(" · ", 1)[0],
                "zone_to": row["cell_to"].split(" · ", 1)[0],
                "cell_from": row["cell_from"],
                "cell_to": row["cell_to"],
                "status": row["status"] or row["operation_type"],
                "initiator": row["initiator"],
                "executor": row["executor"] or row["user"],
                "duration": row["duration"],
            }
        )
    return rows


def _aggregated_stock(params) -> dict[tuple, dict]:
    grouped: dict[tuple, dict] = {}
    for row in _stock_rows(params):
        key = (row["client_name"], row["article"], row["product_name"], row["warehouse_name"])
        current = grouped.setdefault(
            key,
            {
                "client_name": row["client_name"],
                "product_name": row["product_name"],
                "article": row["article"],
                "warehouse_name": row["warehouse_name"],
                "actual_quantity": 0,
                "available_quantity": 0,
                "reserved_quantity": 0,
                "last_movement_at": row["last_movement_at"],
                "cell": row["cell"],
                "batch": row["batch"],
            },
        )
        current["actual_quantity"] += row["actual_quantity"]
        current["available_quantity"] += row["available_quantity"]
        current["reserved_quantity"] += row["reserved_quantity"]
        if row["last_movement_at"] and (not current["last_movement_at"] or row["last_movement_at"] > current["last_movement_at"]):
            current["last_movement_at"] = row["last_movement_at"]
    return grouped


def _shipping_summary(params) -> dict[tuple, dict]:
    qs = (
        ShippingOrderItem.objects.select_related("order", "order__agency")
        .exclude(order__status=ShippingOrder.STATUS_CANCELED)
    )
    qs = _apply_shipping_filters(qs, params)
    summary: dict[tuple, dict] = {}
    for item in qs[:10000]:
        order = item.order
        key = (_agency_name(order.agency), item.sku_code, item.name or item.sku_code, order.destination_warehouse or "")
        current = summary.setdefault(
            key,
            {
                "client_name": _agency_name(order.agency),
                "product_name": item.name or item.sku_code,
                "article": item.sku_code,
                "warehouse_name": order.destination_warehouse or "",
                "order_count": set(),
                "shipping_count": 0,
                "period_outcome": 0,
                "last_outcome_at": None,
                "nearest_shipping_at": None,
            },
        )
        current["order_count"].add(order.number)
        shipped = int(item.qty_shipped or 0)
        current["period_outcome"] += shipped or int(item.qty_requested or 0)
        if shipped:
            current["shipping_count"] += 1
        event_at = order.shipped_at or order.updated_at or order.created_at
        if event_at and (not current["last_outcome_at"] or event_at > current["last_outcome_at"]):
            current["last_outcome_at"] = event_at
        planned = order.eta_at or order.created_at
        if planned and (not current["nearest_shipping_at"] or planned < current["nearest_shipping_at"]):
            current["nearest_shipping_at"] = planned
    for current in summary.values():
        current["order_count"] = len(current["order_count"])
    return summary


def _turnover_rows(params) -> list[dict]:
    stock = _aggregated_stock(params)
    shipping = _shipping_summary(params)
    rows = []
    for key, current in stock.items():
        shipped = _match_shipping(current, shipping)
        average_stock = current["actual_quantity"]
        period_outcome = int(shipped.get("period_outcome") or 0)
        rate = round(period_outcome / average_stock, 2) if average_stock else 0
        days_without = _days_since(current.get("last_movement_at"))
        rows.append(
            {
                "product_name": current["product_name"],
                "article": current["article"],
                "average_stock": average_stock,
                "period_outcome": period_outcome,
                "shipping_count": int(shipped.get("shipping_count") or 0),
                "turnover_rate": rate,
                "average_storage_days": days_without,
                "days_without_movement": days_without,
                "last_income_at": current.get("last_movement_at"),
                "last_outcome_at": shipped.get("last_outcome_at"),
                "turnover_category": _turnover_category(rate, days_without),
            }
        )
    return rows


def _popular_rows(params) -> list[dict]:
    stock = _aggregated_stock(params)
    shipping = sorted(_shipping_summary(params).values(), key=lambda row: row["period_outcome"], reverse=True)
    total = sum(int(row["period_outcome"] or 0) for row in shipping) or 1
    rows = []
    for idx, row in enumerate(shipping, start=1):
        stock_row = _match_stock(row, stock)
        rows.append(
            {
                "rank": idx,
                "product_name": row["product_name"],
                "article": row["article"],
                "order_count": row["order_count"],
                "shipping_count": row["shipping_count"],
                "shipped_quantity": row["period_outcome"],
                "operation_count": row["order_count"] + row["shipping_count"],
                "average_stock": int(stock_row.get("actual_quantity") or 0),
                "volume_share": round((int(row["period_outcome"] or 0) / total) * 100, 2),
                "trend_percent": 0,
            }
        )
    return rows


def _idle_rows(params) -> list[dict]:
    days = _idle_days(params)
    rows = []
    for row in _stock_rows(params):
        idle_for = _days_since(row.get("last_movement_at"))
        if row["actual_quantity"] > 0 and idle_for >= days:
            rows.append(
                {
                    "product_name": row["product_name"],
                    "article": row["article"],
                    "current_quantity": row["actual_quantity"],
                    "warehouse_name": row["warehouse_name"],
                    "cell": row["cell"],
                    "last_movement_at": row["last_movement_at"],
                    "days_without_movement": idle_for,
                    "stock_value": None,
                    "batch": row["batch"],
                    "expiration_at": None,
                    "client_name": row["client_name"],
                }
            )
    return rows


def _shortage_rows(params) -> list[dict]:
    stock = _aggregated_stock(params)
    shipping = _shipping_summary(params)
    rows = []
    for item in shipping.values():
        stock_row = _match_stock(item, stock)
        available = int(stock_row.get("available_quantity") or 0)
        required = int(item.get("period_outcome") or 0)
        shortage = max(required - available, 0)
        if shortage <= 0:
            continue
        rows.append(
            {
                "product_name": item["product_name"],
                "article": item["article"],
                "actual_quantity": int(stock_row.get("actual_quantity") or 0),
                "available_quantity": available,
                "reserved_quantity": int(stock_row.get("reserved_quantity") or 0),
                "minimal_quantity": required,
                "shortage": shortage,
                "active_order_count": int(item.get("order_count") or 0),
                "nearest_shipping_at": item.get("nearest_shipping_at"),
                "recommended_replenishment": shortage,
                "warehouse_name": stock_row.get("warehouse_name") or item.get("warehouse_name") or "",
                "client_name": stock_row.get("client_name") or item.get("client_name") or "",
            }
        )
    return rows


def _excess_rows(params) -> list[dict]:
    shipping = _shipping_summary(params)
    rows = []
    for row in _aggregated_stock(params).values():
        shipped = _match_shipping(row, shipping)
        average_outcome = int(shipped.get("period_outcome") or 0)
        normative = max(average_outcome * 2, 1 if average_outcome else 0)
        excess = max(int(row["actual_quantity"] or 0) - normative, 0)
        if excess <= 0:
            continue
        rows.append(
            {
                "product_name": row["product_name"],
                "article": row["article"],
                "current_quantity": row["actual_quantity"],
                "average_outcome": average_outcome,
                "normative_stock": normative,
                "excess_quantity": excess,
                "forecast_storage_days": 999 if average_outcome == 0 else round(row["actual_quantity"] / max(average_outcome, 1) * 30),
                "excess_value": None,
                "warehouse_name": row["warehouse_name"],
                "client_name": row["client_name"],
                "last_movement_at": row["last_movement_at"],
            }
        )
    return rows


def _expiration_rows(params) -> list[dict]:
    rows = []
    today = timezone.localdate()
    for row in _stock_queryset(params).filter(sku_ref__end_product_date__isnull=False).select_related("sku_ref")[:5000]:
        item = _stock_row(row)
        expiration = row.sku_ref.end_product_date if row.sku_ref else None
        manufactured = row.sku_ref.cr_product_date if row.sku_ref else None
        days = (expiration - today).days if expiration else None
        rows.append(
            {
                "product_name": item["product_name"],
                "article": item["article"],
                "batch": item["batch"],
                "manufactured_at": manufactured,
                "expiration_at": expiration,
                "days_to_expire": days,
                "current_quantity": item["actual_quantity"],
                "warehouse_name": item["warehouse_name"],
                "zone": item["zone"],
                "cell": item["cell"],
                "client_name": item["client_name"],
                "status": _expiration_status(days),
            }
        )
    return rows


def _cell_rows(params) -> list[dict]:
    rows = []
    for row in _stock_rows(params):
        rows.append(
            {
                "warehouse_name": row["warehouse_name"],
                "zone": row["zone"],
                "row": row["row"],
                "rack": row["section"],
                "tier": row["tier"],
                "cell": row["cell"],
                "product_name": row["product_name"],
                "article": row["article"],
                "batch": row["batch"],
                "current_quantity": row["actual_quantity"],
                "occupied_volume": 0,
                "available_volume": 0,
                "fill_percent": 0,
                "last_putaway_at": row["last_movement_at"],
            }
        )
    return rows


def _reserve_rows(params) -> list[dict]:
    qs = WarehouseReserve.objects.select_related("agency", "sku_ref").order_by("-created_at", "-id")
    if params.get("client_id"):
        qs = qs.filter(agency_id=_int_or_none(params.get("client_id")) or 0)
    if params.get("status"):
        qs = qs.filter(status__icontains=str(params.get("status") or "").strip())
    if params.get("sku") or params.get("article"):
        qs = qs.filter(sku_code__icontains=str(params.get("sku") or params.get("article") or "").strip())
    if params.get("barcode"):
        qs = qs.filter(barcode__icontains=str(params.get("barcode") or "").strip())
    if params.get("product"):
        text = str(params.get("product") or "").strip()
        qs = qs.filter(Q(sku_code__icontains=text) | Q(sku_ref__name__icontains=text))
    if params.get("batch"):
        qs = qs.filter(context_id__icontains=str(params.get("batch") or "").strip())
    date_from, date_to = _date_range(params)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    rows = []
    for reserve in qs[:5000]:
        rows.append(
            {
                "reserve_number": reserve.id,
                "created_at": reserve.created_at,
                "order_number": reserve.context_id,
                "client_name": _agency_name(reserve.agency),
                "product_name": getattr(reserve.sku_ref, "name", "") or reserve.sku_code,
                "article": reserve.sku_code,
                "reserved_quantity": int(reserve.qty_reserved or 0),
                "reserved_used": int(reserve.qty_satisfied or 0),
                "reserve_balance": max(int(reserve.qty_reserved or 0) - int(reserve.qty_satisfied or 0), 0),
                "expires_at": None,
                "status": reserve.get_status_display(),
                "warehouse_name": reserve.source_document_type or "",
                "cell": "",
            }
        )
    return rows


def _stock_value_rows(params) -> list[dict]:
    rows = []
    for row in _stock_rows(params):
        rows.append(
            {
                "product_name": row["product_name"],
                "article": row["article"],
                "quantity": row["actual_quantity"],
                "unit_cost": None,
                "total_cost": None,
                "currency": "",
                "warehouse_name": row["warehouse_name"],
                "client_name": row["client_name"],
                "batch": row["batch"],
                "valuation_at": timezone.localtime(),
            }
        )
    return rows


def _apply_shipping_filters(qs, params):
    if params.get("client_id"):
        qs = qs.filter(order__agency_id=_int_or_none(params.get("client_id")) or 0)
    if params.get("sku") or params.get("article"):
        qs = qs.filter(sku_code__icontains=str(params.get("sku") or params.get("article") or "").strip())
    if params.get("barcode"):
        qs = qs.filter(barcode__icontains=str(params.get("barcode") or "").strip())
    if params.get("product"):
        text = str(params.get("product") or "").strip()
        qs = qs.filter(Q(name__icontains=text) | Q(sku_code__icontains=text))
    if params.get("status"):
        qs = qs.filter(order__status__icontains=str(params.get("status") or "").strip())
    date_from, date_to = _date_range(params)
    if date_from:
        qs = qs.filter(order__created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(order__created_at__date__lte=date_to)
    return qs


def _date_range(params):
    date_from = _parse_date(params.get("date_from"))
    date_to = _parse_date(params.get("date_to"))
    period = str(params.get("period") or "").strip()
    if not date_from and period in {"7", "14", "30", "60", "90"}:
        date_from = timezone.localdate() - timedelta(days=int(period))
    return date_from, date_to


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def _agency_name(agency) -> str:
    return str(getattr(agency, "short_name", "") or getattr(agency, "agn_name", "") or agency or "").strip()


def _user_label(user) -> str:
    if not user:
        return ""
    full = str(getattr(user, "get_full_name", lambda: "")() or "").strip()
    return full or str(getattr(user, "username", "") or "").strip()


def _cell_label(location) -> str:
    if not location:
        return ""
    display = str(getattr(location, "display_name", "") or getattr(location, "location_code", "") or "").strip()
    if display:
        return display
    zone = str(getattr(location, "zone_code", "") or "").strip()
    row = int(getattr(location, "row_no", 0) or 0)
    section = int(getattr(location, "section_no", 0) or 0)
    tier = int(getattr(location, "tier_no", 0) or 0)
    cell = int(getattr(location, "cell_no", 0) or 0)
    if any([row, section, tier, cell]):
        return f"{zone} · Ряд {row} · Стеллаж {section} · Ярус {tier} · Ячейка {cell}"
    return zone


def _operation_label(value: str) -> str:
    labels = {
        "receiving_arrived": "Приход",
        "placement_completed": "Размещение",
        "putaway_completed": "Размещение",
        "shipping_reserved": "Резервирование",
        "shipping_reserve_released": "Снятие с резерва",
        "movement_completed": "Перемещение",
        "movement_canceled": "Перемещение отменено",
        "processing_consumed": "Комплектация",
        "loaded_to_vehicle": "Погрузка",
        "shipped": "Отгрузка",
        "stock_returned_to_storage": "Возврат",
    }
    return labels.get(str(value or ""), str(value or ""))


def _duration_label(started_at, completed_at) -> str:
    if not started_at or not completed_at:
        return ""
    seconds = max(int((completed_at - started_at).total_seconds()), 0)
    minutes = seconds // 60
    return f"{minutes} мин." if minutes else f"{seconds} сек."


def _days_since(value) -> int:
    if not value:
        return 0
    current = timezone.localtime(value).date() if hasattr(value, "tzinfo") else value
    return max((timezone.localdate() - current).days, 0)


def _idle_days(params) -> int:
    value = params.get("idle_days") or params.get("period")
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 30


def _turnover_category(rate: float, days_without: int) -> str:
    if rate >= 2:
        return "высокая"
    if rate >= 0.5:
        return "нормальная"
    if days_without >= 60:
        return "неликвид"
    return "низкая"


def _expiration_status(days: int | None) -> str:
    if days is None:
        return ""
    if days < 0:
        return "просрочен"
    if days <= 7:
        return "истекает в течение 7 дней"
    if days <= 14:
        return "истекает в течение 14 дней"
    if days <= 30:
        return "истекает в течение 30 дней"
    return "срок нормальный"


def _match_stock(row: dict, stock: dict[tuple, dict]) -> dict:
    for item in stock.values():
        if item["article"] == row.get("article") and item["client_name"] == row.get("client_name"):
            return item
    return {}


def _match_shipping(row: dict, shipping: dict[tuple, dict]) -> dict:
    for item in shipping.values():
        if item["article"] == row.get("article") and item["client_name"] == row.get("client_name"):
            return item
    return {}
