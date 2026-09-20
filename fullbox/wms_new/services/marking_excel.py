from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from django.utils import timezone


HEADERS = (
    "ID FBS-NEW",
    "Партнер",
    "Товар",
    "Артикул",
    "Штрихкод",
    "Тип кода",
    "Код маркировки",
    "Поступил",
    "Выбыл",
    "Напечатан",
    "Количество печатей",
    "Файл",
    "Обработано",
    "Добавлен",
)


def _excel_datetime(value):
    if not value:
        return None
    if timezone.is_aware(value):
        value = timezone.localtime(value).replace(tzinfo=None)
    return value


def export_marking_codes(queryset) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Коды маркировки FBS-NEW"
    sheet.append(HEADERS)
    for item in queryset.select_related("agency", "product").order_by("-created_at", "-id"):
        sheet.append(
            (
                item.id,
                str(item.agency),
                item.product_name,
                item.article,
                item.barcode,
                item.get_code_type_display(),
                item.code,
                _excel_datetime(item.received_at),
                _excel_datetime(item.retired_at),
                _excel_datetime(item.printed_at),
                item.print_count,
                item.file_name,
                _excel_datetime(item.processed_at),
                _excel_datetime(item.created_at),
            )
        )
    sheet.freeze_panes = "A2"
    for column, width in {
        "A": 14,
        "B": 30,
        "C": 42,
        "D": 22,
        "E": 22,
        "F": 24,
        "G": 58,
        "H": 20,
        "I": 20,
        "J": 20,
        "L": 30,
        "M": 20,
        "N": 20,
    }.items():
        sheet.column_dimensions[column].width = width
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
