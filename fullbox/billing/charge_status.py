"""Статусы начислений для менеджерского реестра (без отдельного enum в БД)."""
from __future__ import annotations

from decimal import Decimal

from .models import ApplicationCharge


NO_PRICE_MESSAGE = (
    "Для услуги не найдена согласованная цена клиента на дату оказания услуги. "
    "Начисление нельзя включить в документ до публикации тарифа бухгалтером."
)


def charge_has_agreed_price(charge: ApplicationCharge) -> bool:
    if charge.is_manual_override:
        return True
    if charge.client_tariff_version_id or getattr(charge, "client_logistics_tariff_id", None):
        return True
    return False


def charge_missing_price(charge: ApplicationCharge) -> bool:
    if getattr(charge, "is_excluded", False):
        return False
    return not charge_has_agreed_price(charge)


def charge_manager_status(charge: ApplicationCharge) -> tuple[str, str, str]:
    """
    Возвращает (code, label, badge).
    badge: gray|blue|orange|red|green
    """
    if getattr(charge, "is_excluded", False):
        return "excluded", "Исключено из расчёта", "gray"
    if charge.is_disputed:
        return "disputed", "Требует корректировки", "red"
    if charge_missing_price(charge):
        return "no_price", "Тариф не найден", "red"
    if charge.service_changed_at and not charge.is_confirmed:
        return "service_changed", "Вид услуги изменён", "orange"
    if charge.original_quantity is not None and charge.original_quantity != charge.quantity and not charge.is_confirmed:
        return "qty_changed", "Количество изменено", "orange"
    if charge.is_included_in_invoice:
        return "in_invoice", "Включено в счёт", "green"
    if charge.is_included_in_act:
        return "in_act", "Включено в акт", "blue"
    if charge.is_confirmed:
        return "confirmed", "Подтверждено", "green"
    if charge.is_manual_override:
        return "override", "Цена изменена вручную", "orange"
    if Decimal(str(charge.tariff or "0")) > 0 and not charge_has_agreed_price(charge):
        return "needs_recalc", "Требуется пересчёт", "orange"
    if Decimal(str(charge.tariff or "0")) > 0:
        return "review", "Требует проверки", "orange"
    return "new", "Требует проверки", "gray"


def charge_source_label(charge: ApplicationCharge) -> str:
    if charge.service_changed_at or charge.previous_service_name:
        return "Изменено менеджером"
    if charge.source_type == ApplicationCharge.SOURCE_MANUAL:
        return "Добавлено менеджером"
    if charge.source_type == ApplicationCharge.SOURCE_WAREHOUSE_APPLICATION:
        return "Источник: автоматически по операции"
    if charge.source_type == ApplicationCharge.SOURCE_STORAGE_DAY:
        return "Источник: хранение"
    return "Источник: автоматически"

