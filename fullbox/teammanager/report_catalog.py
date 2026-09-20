"""Каталог отчётов ЛК менеджера.

Этот модуль намеренно не считает складские данные. Он описывает витрину,
метаданные и безопасную маршрутизацию будущих отчётов, чтобы тяжёлые
read-only селекторы подключались поэтапно и не трогали складскую бизнес-логику.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from urllib.parse import urlencode


@dataclass(frozen=True)
class ReportFilter:
    code: str
    label: str
    type: str = "text"
    required: bool = False
    placeholder: str = ""


@dataclass(frozen=True)
class ReportColumn:
    code: str
    title: str
    type: str = "string"
    sortable: bool = True


@dataclass(frozen=True)
class ReportDefinition:
    code: str
    section: str
    title: str
    description: str
    keywords: tuple[str, ...] = ()
    filters: tuple[ReportFilter, ...] = ()
    columns: tuple[ReportColumn, ...] = ()
    export_formats: tuple[str, ...] = ("XLSX", "CSV")
    status: str = "catalog"
    default_columns: tuple[str, ...] = ()
    direct_url: str = ""

    @property
    def url(self) -> str:
        return self.direct_url or f"/team-manager/reports/{self.section}/{self.code}/"


@dataclass(frozen=True)
class ReportSection:
    key: str
    title: str
    description: str
    icon: str
    reports: tuple[ReportDefinition, ...] = field(default_factory=tuple)
    roles: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return f"/team-manager/reports/{self.key}/"


# Внутреннее правило FULLBOX: head_manager относится к главному/складскому
# кабинету. Когда задача говорит «ЛК менеджеров», новые функции не расширяем
# на head_manager без отдельного согласования.
MANAGER_REPORT_ROLES = ("manager", "logistician", "director", "admin", "developer")
FINANCE_REPORT_ROLES = ("manager", "director", "admin", "developer")


COMMON_FILTERS = (
    ReportFilter("date_from", "Дата с", "date"),
    ReportFilter("date_to", "Дата по", "date"),
    ReportFilter("client_id", "Клиент", "lookup", placeholder="Название или ИНН клиента"),
    ReportFilter("warehouse_id", "Склад", "lookup", placeholder="Название склада"),
)

STOCK_FILTERS = COMMON_FILTERS + (
    ReportFilter("zone", "Зона", "text"),
    ReportFilter("cell", "Ячейка", "text"),
    ReportFilter("sku", "SKU / артикул", "text"),
    ReportFilter("barcode", "Штрихкод", "text"),
    ReportFilter("only_positive", "Только положительный остаток", "checkbox"),
    ReportFilter("only_available", "Только доступный остаток", "checkbox"),
)

STOCK_COLUMNS = (
    ReportColumn("client_name", "Клиент"),
    ReportColumn("product_name", "Товар"),
    ReportColumn("sku", "SKU"),
    ReportColumn("barcode", "Штрихкод"),
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("zone", "Зона"),
    ReportColumn("cell", "Ячейка"),
    ReportColumn("total_quantity", "Общий остаток", "number"),
    ReportColumn("reserved_quantity", "Резерв", "number"),
    ReportColumn("blocked_quantity", "Заблокировано", "number"),
    ReportColumn("available_quantity", "Доступно", "number"),
    ReportColumn("last_operation_at", "Последняя операция", "datetime"),
)

PRODUCT_FILTERS = (
    ReportFilter("date_from", "Дата с", "date"),
    ReportFilter("date_to", "Дата по", "date"),
    ReportFilter("period", "Период / дней без движения", "text", placeholder="7, 14, 30, 60, 90"),
    ReportFilter("client_id", "Клиент", "lookup", placeholder="ID клиента"),
    ReportFilter("warehouse_id", "Склад", "lookup", placeholder="Код или название склада"),
    ReportFilter("zone", "Зона", "text"),
    ReportFilter("cell", "Ячейка", "text"),
    ReportFilter("product", "Товар", "text", placeholder="Название товара"),
    ReportFilter("category", "Категория", "text"),
    ReportFilter("article", "Артикул", "text"),
    ReportFilter("sku", "SKU", "text"),
    ReportFilter("barcode", "Штрихкод", "text"),
    ReportFilter("batch", "Партия", "text"),
    ReportFilter("status", "Статус", "text"),
    ReportFilter("operation_type", "Тип операции", "text"),
    ReportFilter("responsible", "Ответственный", "text"),
    ReportFilter("stock_type", "Тип остатка", "text", placeholder="available / reserved / blocked"),
    ReportFilter("has_reserve", "Наличие резерва", "checkbox"),
    ReportFilter("only_positive", "Только товары с остатком", "checkbox"),
    ReportFilter("only_zero", "Только нулевые остатки", "checkbox"),
)

PRODUCT_BALANCE_FILTERS = (
    ReportFilter("client_id", "Клиент", "lookup", required=True, placeholder="Выберите клиента"),
    ReportFilter("sku", "Артикул / SKU", "text", required=True, placeholder="Точное значение, например S002"),
    ReportFilter("date_from", "Дата движения с", "date"),
    ReportFilter("date_to", "Дата движения по", "date"),
)

PRODUCT_STOCK_COLUMNS = (
    ReportColumn("article", "Артикул"),
    ReportColumn("sku", "SKU"),
    ReportColumn("barcode", "Штрихкод"),
    ReportColumn("product_name", "Название товара"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("zone", "Зона"),
    ReportColumn("cell", "Ячейка"),
    ReportColumn("batch", "Партия"),
    ReportColumn("actual_quantity", "Фактический остаток", "number"),
    ReportColumn("available_quantity", "Доступный остаток", "number"),
    ReportColumn("reserved_quantity", "Резерв", "number"),
    ReportColumn("blocked_quantity", "Заблокированный остаток", "number"),
    ReportColumn("unit", "Единица измерения"),
    ReportColumn("last_movement_at", "Дата последнего движения", "datetime"),
)

PRODUCT_MOVEMENT_COLUMNS = (
    ReportColumn("operation_at", "Дата и время", "datetime"),
    ReportColumn("event_label", "Событие"),
    ReportColumn("direction", "Направление"),
    ReportColumn("document_type", "Тип документа"),
    ReportColumn("document_number", "Номер документа"),
    ReportColumn("article", "Артикул"),
    ReportColumn("barcode", "Штрихкод"),
    ReportColumn("product_name", "Товар"),
    ReportColumn("operation_quantity", "Количество", "number"),
    ReportColumn("cell_from", "Откуда"),
    ReportColumn("cell_to", "Куда"),
    ReportColumn("pallet_code", "Паллета"),
    ReportColumn("box_code", "Короб"),
    ReportColumn("user", "Сотрудник"),
    ReportColumn("performed_by_role", "Роль"),
    ReportColumn("comment", "Комментарий"),
)

PRODUCT_BALANCE_COLUMNS = (
    ReportColumn("operation_at", "Дата и время", "datetime"),
    ReportColumn("operation_type", "Операция"),
    ReportColumn("document_number", "Документ"),
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("incoming_quantity", "Приход", "number"),
    ReportColumn("outgoing_quantity", "Расход", "number"),
    ReportColumn("pallet_code", "Паллета"),
    ReportColumn("box_code", "Короб"),
    ReportColumn("location", "Место"),
    ReportColumn("responsible", "Ответственный"),
)

PRODUCT_BOX_FLOW_COLUMNS = (
    ReportColumn("number", "№", "number"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("pallet_code", "Паллет №"),
    ReportColumn("box_code", "Короб №"),
    ReportColumn("sku", "SKU"),
    ReportColumn("product_name", "Номенклатура"),
    ReportColumn("barcode", "Штрихкод"),
    ReportColumn("quantity", "Количество, шт", "number"),
    ReportColumn("status", "Статус"),
    ReportColumn("width_mm", "Ширина, мм", "number"),
    ReportColumn("height_mm", "Высота, мм", "number"),
    ReportColumn("depth_mm", "Глубина, мм", "number"),
    ReportColumn("volume_m3", "Объем, м³", "number"),
    ReportColumn("weight_g", "Вес, г", "number"),
    ReportColumn("income_at", "Приход дата", "date"),
    ReportColumn("income_document", "Номер приходного документа"),
    ReportColumn("outcome_at", "Расход дата", "date"),
    ReportColumn("outcome_document", "Номер расходного документа"),
)

PRODUCT_INCOME_COLUMNS = (
    ReportColumn("income_at", "Дата прихода", "datetime"),
    ReportColumn("receiving_number", "Номер приёмки"),
    ReportColumn("source", "Поставщик или источник"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("accepted_quantity", "Принято", "number"),
    ReportColumn("shortage_quantity", "Недостача", "number"),
    ReportColumn("surplus_quantity", "Излишек", "number"),
    ReportColumn("defect_quantity", "Брак", "number"),
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("zone", "Зона"),
    ReportColumn("cell", "Ячейка"),
    ReportColumn("batch", "Партия"),
    ReportColumn("expiration_at", "Срок годности", "date"),
    ReportColumn("responsible", "Ответственный сотрудник"),
)

PRODUCT_OUTCOME_COLUMNS = (
    ReportColumn("date", "Дата", "datetime"),
    ReportColumn("outcome_type", "Тип расхода"),
    ReportColumn("document_number", "Номер документа"),
    ReportColumn("order_number", "Заказ"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("quantity", "Количество", "number"),
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("cell", "Ячейка"),
    ReportColumn("reason", "Причина расхода"),
    ReportColumn("recipient", "Получатель"),
    ReportColumn("responsible", "Ответственный сотрудник"),
)

PRODUCT_TRANSFER_COLUMNS = (
    ReportColumn("date", "Дата", "datetime"),
    ReportColumn("transfer_number", "Номер перемещения"),
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("quantity", "Количество", "number"),
    ReportColumn("warehouse_from", "Склад-источник"),
    ReportColumn("warehouse_to", "Склад-получатель"),
    ReportColumn("zone_from", "Зона-источник"),
    ReportColumn("zone_to", "Зона-получатель"),
    ReportColumn("cell_from", "Ячейка-источник"),
    ReportColumn("cell_to", "Ячейка-получатель"),
    ReportColumn("status", "Статус"),
    ReportColumn("initiator", "Инициатор"),
    ReportColumn("executor", "Исполнитель"),
    ReportColumn("duration", "Время выполнения"),
)

PRODUCT_TURNOVER_COLUMNS = (
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("average_stock", "Средний остаток", "number"),
    ReportColumn("period_outcome", "Расход за период", "number"),
    ReportColumn("shipping_count", "Количество отгрузок", "number"),
    ReportColumn("turnover_rate", "Коэффициент оборачиваемости", "number"),
    ReportColumn("average_storage_days", "Средний срок хранения", "number"),
    ReportColumn("days_without_movement", "Дни без движения", "number"),
    ReportColumn("last_income_at", "Последняя дата прихода", "datetime"),
    ReportColumn("last_outcome_at", "Последняя дата расхода", "datetime"),
    ReportColumn("turnover_category", "Категория оборачиваемости"),
)

PRODUCT_POPULAR_COLUMNS = (
    ReportColumn("rank", "Место в рейтинге", "number"),
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("order_count", "Количество заказов", "number"),
    ReportColumn("shipping_count", "Количество отгрузок", "number"),
    ReportColumn("shipped_quantity", "Отгружено единиц", "number"),
    ReportColumn("operation_count", "Количество операций", "number"),
    ReportColumn("average_stock", "Средний остаток", "number"),
    ReportColumn("volume_share", "Доля в общем объёме", "number"),
    ReportColumn("trend_percent", "Динамика относительно прошлого периода", "number"),
)

PRODUCT_IDLE_COLUMNS = (
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("current_quantity", "Текущий остаток", "number"),
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("cell", "Ячейка"),
    ReportColumn("last_movement_at", "Дата последнего движения", "datetime"),
    ReportColumn("days_without_movement", "Дней без движения", "number"),
    ReportColumn("stock_value", "Стоимость остатка", "number"),
    ReportColumn("batch", "Партия"),
    ReportColumn("expiration_at", "Срок годности", "date"),
    ReportColumn("client_name", "Клиент"),
)

PRODUCT_SHORTAGE_COLUMNS = (
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("actual_quantity", "Фактический остаток", "number"),
    ReportColumn("available_quantity", "Доступный остаток", "number"),
    ReportColumn("reserved_quantity", "Резерв", "number"),
    ReportColumn("minimal_quantity", "Минимальный остаток", "number"),
    ReportColumn("shortage", "Дефицит", "number"),
    ReportColumn("active_order_count", "Количество активных заказов", "number"),
    ReportColumn("nearest_shipping_at", "Ближайшая дата отгрузки", "datetime"),
    ReportColumn("recommended_replenishment", "Рекомендованное пополнение", "number"),
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("client_name", "Клиент"),
)

PRODUCT_EXCESS_COLUMNS = (
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("current_quantity", "Текущий остаток", "number"),
    ReportColumn("average_outcome", "Средний расход", "number"),
    ReportColumn("normative_stock", "Нормативный запас", "number"),
    ReportColumn("excess_quantity", "Излишек", "number"),
    ReportColumn("forecast_storage_days", "Прогнозный срок хранения", "number"),
    ReportColumn("excess_value", "Стоимость излишка", "number"),
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("last_movement_at", "Дата последнего движения", "datetime"),
)

PRODUCT_EXPIRATION_COLUMNS = (
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("batch", "Партия"),
    ReportColumn("manufactured_at", "Дата производства", "date"),
    ReportColumn("expiration_at", "Срок годности", "date"),
    ReportColumn("days_to_expire", "Дней до окончания", "number"),
    ReportColumn("current_quantity", "Остаток", "number"),
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("zone", "Зона"),
    ReportColumn("cell", "Ячейка"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("status", "Статус"),
)

PRODUCT_CELL_COLUMNS = (
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("zone", "Зона"),
    ReportColumn("row", "Ряд", "number"),
    ReportColumn("rack", "Стеллаж", "number"),
    ReportColumn("tier", "Ярус", "number"),
    ReportColumn("cell", "Ячейка"),
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("batch", "Партия"),
    ReportColumn("current_quantity", "Остаток", "number"),
    ReportColumn("occupied_volume", "Занятый объём", "number"),
    ReportColumn("available_volume", "Доступный объём", "number"),
    ReportColumn("fill_percent", "Процент заполнения", "number"),
    ReportColumn("last_putaway_at", "Дата последнего размещения", "datetime"),
)

PRODUCT_RESERVE_COLUMNS = (
    ReportColumn("reserve_number", "Номер резерва"),
    ReportColumn("created_at", "Дата создания", "datetime"),
    ReportColumn("order_number", "Заказ"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("reserved_quantity", "Зарезервировано", "number"),
    ReportColumn("reserved_used", "Использовано", "number"),
    ReportColumn("reserve_balance", "Остаток резерва", "number"),
    ReportColumn("expires_at", "Дата окончания", "datetime"),
    ReportColumn("status", "Статус"),
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("cell", "Ячейка"),
)

PRODUCT_VALUE_COLUMNS = (
    ReportColumn("product_name", "Товар"),
    ReportColumn("article", "Артикул"),
    ReportColumn("quantity", "Количество", "number"),
    ReportColumn("unit_cost", "Закупочная стоимость единицы", "number"),
    ReportColumn("total_cost", "Общая стоимость", "number"),
    ReportColumn("currency", "Валюта"),
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("batch", "Партия"),
    ReportColumn("valuation_at", "Дата оценки", "datetime"),
)


def _make_reports(
    section: str,
    specs: tuple[tuple[str, str, str, tuple[str, ...]], ...],
    *,
    default_filters: tuple[ReportFilter, ...] = COMMON_FILTERS,
    default_columns: tuple[ReportColumn, ...] | None = None,
) -> tuple[ReportDefinition, ...]:
    reports: list[ReportDefinition] = []
    for code, title, description, keywords in specs:
        filters = default_filters
        columns = default_columns or (
            ReportColumn("name", "Название"),
            ReportColumn("value", "Значение", "number"),
            ReportColumn("updated_at", "Обновлено", "datetime"),
        )
        if section == "warehouse" and code in {"current-stock", "available-stock", "stock-by-date"}:
            filters = STOCK_FILTERS
            columns = STOCK_COLUMNS
        reports.append(
            ReportDefinition(
                code=code,
                section=section,
                title=title,
                description=description,
                keywords=keywords,
                filters=filters,
                columns=columns,
            )
        )
    return tuple(reports)


CLIENT_REPORTS = _make_reports(
    "clients",
    (
        ("client-summary", "Сводка по клиентам", "Ключевые показатели по клиентам: заявки, остатки, услуги и финансы.", ("клиент", "сводка", "остатки", "услуги")),
        ("client-card", "Карточка аналитики клиента", "Детальная аналитика выбранного клиента по складским и финансовым блокам.", ("клиент", "карточка", "аналитика")),
        ("client-activity", "Активность клиентов", "Динамика заявок, операций и обращений клиентов за период.", ("клиент", "активность", "операции")),
        ("client-stock", "Остатки по клиентам", "Свод доступных и зарезервированных остатков в разрезе клиентов.", ("остатки", "клиент", "доступно")),
        ("client-movement", "Движение товаров по клиентам", "Приход, расход и перемещения товаров по клиентам.", ("движение", "клиент", "товар")),
        ("client-operations", "Операции по клиентам", "Все операции и заявки клиента с текущими статусами.", ("операции", "заявки", "статусы")),
        ("client-services", "Услуги по клиентам", "Начисленные и оказанные услуги по каждому клиенту.", ("услуги", "начисления")),
        ("client-sla", "SLA по клиентам", "Сроки реакции и выполнения операций в разрезе клиентов.", ("sla", "срок", "просрочка")),
        ("client-debt", "Задолженность клиентов", "Счета, оплаты и просроченная задолженность.", ("задолженность", "счета", "оплаты")),
        ("client-rating", "Рейтинг клиентов", "Рейтинг клиентов по объёму операций, заявкам и финансовым показателям.", ("рейтинг", "клиенты")),
        ("client-slow-stock", "Неликвидные товары по клиентам", "Товары клиентов без движения за выбранный период.", ("неликвид", "без движения", "клиент")),
        ("client-discrepancies", "Расхождения по клиентам", "Расхождения при приёмке, отгрузке и учётных сверках.", ("расхождения", "клиент")),
    ),
)

PRODUCT_REPORTS = (
    ReportDefinition(
        "product-stock",
        "products",
        "Остатки товаров",
        "Фактические, доступные и зарезервированные остатки товаров.",
        ("остатки", "товар", "доступно", "резерв", "ячейки", "тип остатка"),
        PRODUCT_FILTERS,
        PRODUCT_STOCK_COLUMNS,
    ),
    ReportDefinition(
        "product-movement",
        "products",
        "Движение товара",
        "История всех операций по товару за выбранный период.",
        ("движение", "операции", "приход", "расход", "резервирование", "корректировка", "возврат", "комплектация", "отгрузка"),
        PRODUCT_FILTERS,
        PRODUCT_MOVEMENT_COLUMNS,
        default_columns=(
            "operation_at",
            "event_label",
            "direction",
            "document_type",
            "document_number",
            "article",
            "product_name",
            "operation_quantity",
            "cell_from",
            "cell_to",
        ),
    ),
    ReportDefinition(
        "product-income-outcome-stock",
        "products",
        "Приход, расход и остаток",
        "Сверка фактического прихода, расхода, текущего остатка и активного резерва по клиенту и точному артикулу.",
        ("приход", "расход", "остаток", "сверка", "резерв", "клиент", "артикул"),
        PRODUCT_BALANCE_FILTERS,
        PRODUCT_BALANCE_COLUMNS,
        ("XLSX", "CSV"),
    ),
    ReportDefinition(
        "product-box-flow",
        "products",
        "Движение товара по коробам",
        "Коробовой отчет: когда товар пришел, куда ушел и где остался.",
        ("короб", "паллет", "движение", "приход", "расход", "застрял", "остаток", "шаблон"),
        PRODUCT_FILTERS,
        PRODUCT_BOX_FLOW_COLUMNS,
        ("XLSX", "CSV"),
    ),
    ReportDefinition(
        "product-income",
        "products",
        "Приход товара",
        "Приходные операции и источники поступления.",
        ("приход", "приёмка", "поставка", "источник", "срок годности"),
        PRODUCT_FILTERS,
        PRODUCT_INCOME_COLUMNS,
    ),
    ReportDefinition(
        "product-outcome",
        "products",
        "Расход товара",
        "Расходные операции, отгрузки и списания.",
        ("расход", "отгрузка", "списание", "заказ", "получатель"),
        PRODUCT_FILTERS,
        PRODUCT_OUTCOME_COLUMNS,
    ),
    ReportDefinition(
        "product-transfers",
        "products",
        "Перемещения товара",
        "Перемещения товара между складами, зонами и ячейками.",
        ("перемещения", "ячейки", "зоны", "склад", "статус"),
        PRODUCT_FILTERS,
        PRODUCT_TRANSFER_COLUMNS,
    ),
    ReportDefinition(
        "turnover",
        "products",
        "Оборачиваемость товаров",
        "Скорость движения и срок хранения товаров.",
        ("оборачиваемость", "хранение", "неликвид", "скорость", "категория"),
        PRODUCT_FILTERS,
        PRODUCT_TURNOVER_COLUMNS,
    ),
    ReportDefinition(
        "popular-products",
        "products",
        "Популярные товары",
        "Товары с наибольшим количеством операций и отгрузок.",
        ("популярные", "рейтинг", "заказы", "отгрузки", "доля"),
        PRODUCT_FILTERS,
        PRODUCT_POPULAR_COLUMNS,
    ),
    ReportDefinition(
        "idle-products",
        "products",
        "Товары без движения",
        "Товары с положительным остатком, по которым долго не было операций.",
        ("без движения", "неликвид", "7 дней", "14 дней", "30 дней", "60 дней", "90 дней"),
        PRODUCT_FILTERS,
        PRODUCT_IDLE_COLUMNS,
    ),
    ReportDefinition(
        "shortage-products",
        "products",
        "Дефицит товаров",
        "Позиции с недостаточным доступным остатком.",
        ("дефицит", "минимальный остаток", "активные заказы", "пополнение"),
        PRODUCT_FILTERS,
        PRODUCT_SHORTAGE_COLUMNS,
    ),
    ReportDefinition(
        "excess-stock",
        "products",
        "Избыточные остатки",
        "Позиции с избыточным складским запасом.",
        ("избыточные", "излишки", "нормативный запас", "средний расход"),
        PRODUCT_FILTERS,
        PRODUCT_EXCESS_COLUMNS,
    ),
    ReportDefinition(
        "expiration-dates",
        "products",
        "Сроки годности",
        "Контроль партий и товаров с приближающимся сроком годности.",
        ("срок годности", "партии", "просрочен", "истекает", "30 дней", "14 дней", "7 дней"),
        PRODUCT_FILTERS,
        PRODUCT_EXPIRATION_COLUMNS,
    ),
    ReportDefinition(
        "product-cells",
        "products",
        "Товары по ячейкам",
        "Размещение товаров по складам, зонам и ячейкам.",
        ("ячейки", "размещение", "склад", "зона", "ряд", "стеллаж"),
        PRODUCT_FILTERS,
        PRODUCT_CELL_COLUMNS,
    ),
    ReportDefinition(
        "product-reserves",
        "products",
        "Резервы товаров",
        "Активные, использованные и просроченные резервы.",
        ("резервы", "активен", "использован", "отменён", "просрочен"),
        PRODUCT_FILTERS,
        PRODUCT_RESERVE_COLUMNS,
    ),
    ReportDefinition(
        "stock-value",
        "products",
        "Стоимость товарных остатков",
        "Оценка стоимости товаров на складе.",
        ("стоимость", "закупочная стоимость", "валюта", "остатки", "оценка"),
        PRODUCT_FILTERS,
        PRODUCT_VALUE_COLUMNS,
    ),
)

NOMENCLATURE_REPORTS = _make_reports(
    "nomenclature",
    (
        ("catalog", "Справочник номенклатуры", "Список SKU, артикулов, карточек и связей.", ("номенклатура", "sku", "артикул")),
        ("sku-stock", "Остатки по SKU", "Остатки в разрезе SKU и штрихкодов.", ("sku", "остатки")),
        ("sku-movement", "Движение по SKU", "Движение и операции по SKU.", ("sku", "движение")),
        ("article-stock", "Остатки по артикулам", "Остатки в разрезе клиентских артикулов.", ("артикул", "остатки")),
        ("batches", "Партии и серии", "Партии, серии и связанные остатки.", ("партия", "серия")),
        ("expiration-dates", "Сроки годности", "Сроки годности и группы риска по товару.", ("срок годности", "просрочено")),
        ("barcodes", "Штрихкоды", "Штрихкоды товаров и связанная номенклатура.", ("штрихкод", "barcode")),
        ("idle-nomenclature", "Номенклатура без движения", "SKU без движения за выбранный период.", ("без движения", "sku")),
        ("duplicates", "Дубли номенклатуры", "Потенциальные дубли карточек и штрихкодов.", ("дубли", "ошибки")),
        ("card-errors", "Ошибки карточек номенклатуры", "Карточки с неполными или конфликтными данными.", ("ошибки", "карточки")),
        ("weight-volume", "Вес и объём", "Весогабаритные характеристики SKU.", ("вес", "объем", "габариты")),
        ("without-barcode", "Номенклатура без штрихкода", "Карточки без основного штрихкода.", ("без штрихкода",)),
        ("without-dimensions", "Номенклатура без габаритов", "Карточки без веса или размеров.", ("без габаритов",)),
        ("without-category", "Номенклатура без категории", "Карточки без категории или типа товара.", ("без категории",)),
        ("archived-with-stock", "Архивная номенклатура с остатками", "Архивные позиции, по которым ещё есть остаток.", ("архив", "остатки")),
    ),
)

WAREHOUSE_REPORTS = _make_reports(
    "warehouse",
    (
        ("current-stock", "Остатки на текущую дату", "Фактические остатки на момент формирования отчёта.", ("остатки", "доступно", "резерв")),
        ("stock-by-date", "Остатки на выбранную дату", "Остаток на конец выбранной даты по складским операциям.", ("остатки", "дата", "история")),
        ("stock-by-warehouse", "Остатки по складам", "Остатки в разрезе складов.", ("остатки", "склады")),
        ("stock-by-zone", "Остатки по зонам", "Остатки по зонам хранения.", ("зоны", "остатки")),
        ("stock-by-cell", "Остатки по ячейкам", "Остатки по ячейкам и местам хранения.", ("ячейки", "адресное хранение")),
        ("stock-by-client", "Остатки по клиентам", "Остатки склада в разрезе клиентов.", ("клиент", "остатки")),
        ("stock-by-product", "Остатки по товарам", "Остатки склада в разрезе товаров.", ("товар", "остатки")),
        ("stock-movement", "Движение товаров", "Приход, расход, перемещения и корректировки.", ("движение", "операции")),
        ("turnover-balance", "Оборотно-сальдовая ведомость", "Начальный остаток, приход, расход и конечный остаток.", ("ведомость", "оборот", "сальдо")),
        ("warehouse-load", "Загрузка склада", "Занятость склада по местам, весу и объёму.", ("загрузка", "склад")),
        ("zone-load", "Загрузка складских зон", "Занятость складских зон.", ("загрузка", "зоны")),
        ("cell-load", "Загрузка ячеек", "Занятость и свободные места ячеек.", ("загрузка", "ячейки")),
        ("empty-cells", "Пустые ячейки", "Свободные ячейки и места хранения.", ("пустые", "ячейки")),
        ("blocked-cells", "Заблокированные ячейки", "Ячейки, недоступные для операций.", ("заблокированные", "ячейки")),
        ("stock-without-cell", "Товары без ячейки", "Остатки без адресного размещения.", ("без ячейки", "ошибка")),
        ("multi-product-cells", "Несколько товаров в одной ячейке", "Ячейки со смешанным хранением.", ("микс", "ячейки")),
        ("receiving", "Приёмка", "Складские показатели по приёмке.", ("приемка", "приёмка")),
        ("receiving-discrepancies", "Расхождения при приёмке", "Расхождения факта и плана при приёмке.", ("расхождения", "приемка")),
        ("shipping", "Отгрузка", "Складские показатели по отгрузке.", ("отгрузка",)),
        ("shipping-discrepancies", "Расхождения при отгрузке", "Расхождения факта и плана при отгрузке.", ("расхождения", "отгрузка")),
        ("transfers", "Перемещения", "Перемещения между зонами, ячейками и складами.", ("перемещения",)),
        ("inventory", "Инвентаризация", "Проведённые и активные инвентаризации.", ("инвентаризация",)),
        ("inventory-results", "Результаты инвентаризации", "Итоги пересчётов и сверок.", ("инвентаризация", "результаты")),
        ("surplus", "Излишки", "Позиции с положительным расхождением.", ("излишки",)),
        ("shortage", "Недостачи", "Позиции с отрицательным расхождением.", ("недостачи",)),
        ("idle-stock", "Товары без движения", "Остатки без движения за выбранный период.", ("без движения", "неликвид")),
        ("expiration-dates", "Сроки годности", "Сроки годности по складским остаткам.", ("срок годности",)),
        ("stock-discrepancies", "Расхождения остатков", "Несоответствия учётных и фактических остатков.", ("расхождения", "остатки")),
        ("blocked-stock", "Заблокированные остатки", "Остатки, недоступные к операциям.", ("блокировка", "остатки")),
        ("reserves", "Резервы товаров", "Зарезервированные количества по заявкам.", ("резерв", "отгрузка")),
        ("available-stock", "Доступные остатки", "Остатки без резервов и блокировок.", ("доступно", "остатки")),
        ("warehouse-operation-history", "История складских операций", "Журнал складских операций и событий.", ("история", "операции")),
    ),
)

OPERATIONS_REPORTS = _make_reports(
    "operations",
    (
        ("employee-productivity", "Производительность сотрудников", "Количество и скорость операций по сотрудникам.", ("сотрудник", "производительность")),
        ("shift-productivity", "Производительность по сменам", "Операционные показатели по сменам.", ("смена", "производительность")),
        ("employee-operation-count", "Количество операций по сотрудникам", "Количество операций и задач на сотрудника.", ("операции", "сотрудник")),
        ("avg-receiving-time", "Среднее время приёмки", "Время от создания до завершения приёмки.", ("приемка", "время")),
        ("avg-placement-time", "Среднее время размещения", "Скорость размещения товара.", ("размещение", "время")),
        ("avg-picking-time", "Среднее время подбора", "Скорость подбора товара к отгрузке.", ("подбор", "время")),
        ("avg-packing-time", "Среднее время упаковки", "Скорость упаковки операций.", ("упаковка", "время")),
        ("avg-shipping-time", "Среднее время отгрузки", "Время подготовки и отгрузки.", ("отгрузка", "время")),
        ("operation-sla", "SLA операций", "Соблюдение сроков по типам операций.", ("sla", "просрочка")),
        ("overdue-operations", "Просроченные операции", "Операции с нарушенным сроком.", ("просрочка",)),
        ("unfinished-operations", "Незавершённые операции", "Операции без завершения.", ("незавершенные",)),
        ("stale-operations", "Операции без движения", "Операции, которые не менялись дольше нормы.", ("без движения", "зависло")),
        ("warehouse-task-queue", "Очередь складских заданий", "Очередь заданий по операционным зонам.", ("очередь", "задания")),
        ("load-by-hours", "Загрузка по часам", "Распределение операций по часам.", ("часы", "загрузка")),
        ("load-by-days", "Загрузка по дням", "Распределение операций по дням.", ("дни", "загрузка")),
        ("picking-errors", "Ошибки комплектации", "Ошибки подбора и комплектации.", ("ошибки", "комплектация")),
        ("packing-errors", "Ошибки упаковки", "Ошибки упаковки и маркировки.", ("ошибки", "упаковка")),
        ("mis-sorting", "Пересорт", "События пересорта и выявленные причины.", ("пересорт",)),
        ("cancelled-operations", "Отменённые операции", "Отмены операций и причины.", ("отмена",)),
        ("repeated-operations", "Повторные операции", "Операции, выполненные повторно.", ("повтор",)),
        ("employee-quality", "Качество работы сотрудников", "Качество операций и доля ошибок.", ("качество", "сотрудник")),
    ),
)

FINANCE_REPORTS = _make_reports(
    "finance",
    (
        ("charges-by-client", "Начисления по клиентам", "Начисления и услуги в разрезе клиентов.", ("начисления", "клиент")),
        ("charges-by-service", "Начисления по услугам", "Начисления по видам услуг.", ("услуги", "начисления")),
        ("charges-by-warehouse", "Начисления по складам", "Начисления в разрезе складов.", ("склады", "начисления")),
        ("storage-cost", "Стоимость хранения", "Стоимость хранения за период.", ("хранение", "стоимость")),
        ("receiving-cost", "Стоимость приёмки", "Стоимость операций приёмки.", ("приемка", "стоимость")),
        ("shipping-cost", "Стоимость отгрузки", "Стоимость операций отгрузки.", ("отгрузка", "стоимость")),
        ("processing-cost", "Стоимость обработки", "Стоимость операций обработки.", ("обработка", "стоимость")),
        ("packing-cost", "Стоимость упаковки", "Стоимость упаковки.", ("упаковка", "стоимость")),
        ("extra-services-cost", "Стоимость дополнительных услуг", "Дополнительные услуги и начисления.", ("дополнительные услуги",)),
        ("tariff-details", "Расшифровка тарификации", "Тарифы, правила и детализация расчёта.", ("тариф", "расшифровка")),
        ("invoices", "Счета", "Счета и их статусы.", ("счета",)),
        ("payments", "Оплаты", "Поступившие оплаты и привязка к счетам.", ("оплаты",)),
        ("client-debt", "Задолженность клиентов", "Текущая задолженность клиентов.", ("задолженность",)),
        ("overdue-debt", "Просроченная задолженность", "Задолженность с истёкшим сроком оплаты.", ("просроченная", "задолженность")),
        ("unpaid-services", "Неоплаченные услуги", "Услуги без оплаты или счета.", ("неоплаченные", "услуги")),
        ("plan-fact-charges", "План-факт начислений", "Сравнение плановых и фактических начислений.", ("план", "факт")),
        ("charge-corrections", "Корректировки начислений", "Корректировки, причины и авторы.", ("корректировки",)),
    ),
)


# Публичный каталог содержит только отчёты, которые действительно строятся
# из read-only источников.  Старые декларации выше оставлены как история
# проектирования, но не попадают в SECTIONS и не создают пустые страницы.
PRODUCT_REPORTS = tuple(
    replace(report, status="live")
    for report in PRODUCT_REPORTS
    if report.code != "stock-value"  # Закупочной стоимости в канонической модели пока нет.
)
_PRODUCT_BY_CODE = {report.code: report for report in PRODUCT_REPORTS}

CLIENT_ANALYTICS_FILTERS = (
    ReportFilter("date_from", "Дата с", "date"),
    ReportFilter("date_to", "Дата по", "date"),
    ReportFilter("client_id", "Клиент", "lookup", placeholder="Выберите клиента"),
)
NOMENCLATURE_FILTERS = (
    ReportFilter("client_id", "Клиент", "lookup", placeholder="Выберите клиента"),
    ReportFilter("product", "Название товара", "text"),
    ReportFilter("category", "Категория", "text"),
    ReportFilter("sku", "Артикул / SKU", "text"),
    ReportFilter("barcode", "Штрихкод", "text"),
)
LOCATION_FILTERS = (
    ReportFilter("warehouse_id", "Склад", "lookup", placeholder="Код или название склада"),
    ReportFilter("zone", "Зона", "text"),
    ReportFilter("cell", "Ячейка", "text"),
)
OPERATION_FILTERS = (
    ReportFilter("date_from", "Дата с", "date"),
    ReportFilter("date_to", "Дата по", "date"),
    ReportFilter("period", "Период, дней", "text", placeholder="7, 14, 30, 60 или 90"),
    ReportFilter("client_id", "Клиент", "lookup", placeholder="Выберите клиента"),
    ReportFilter("operation_type", "Тип операции", "text"),
    ReportFilter("status", "Статус", "text"),
)
FINANCE_FILTERS = (
    ReportFilter("date_from", "Дата с", "date"),
    ReportFilter("date_to", "Дата по", "date"),
    ReportFilter("period", "Период, дней", "text", placeholder="7, 14, 30, 60 или 90"),
    ReportFilter("client_id", "Клиент", "lookup", placeholder="Выберите клиента"),
)


def _live_report(
    code: str,
    section: str,
    title: str,
    description: str,
    keywords: tuple[str, ...],
    filters: tuple[ReportFilter, ...],
    columns: tuple[ReportColumn, ...],
) -> ReportDefinition:
    return ReportDefinition(
        code,
        section,
        title,
        description,
        keywords,
        filters,
        columns,
        status="live",
    )


def _product_alias(
    section: str,
    code: str,
    source_code: str,
    title: str,
    description: str,
    keywords: tuple[str, ...],
    direct_url: str = "",
) -> ReportDefinition:
    source = _PRODUCT_BY_CODE[source_code]
    return ReportDefinition(
        code,
        section,
        title,
        description,
        keywords,
        STOCK_FILTERS if section == "warehouse" and source_code == "product-stock" else source.filters,
        source.columns,
        source.export_formats,
        "live",
        source.default_columns,
        direct_url,
    )


CLIENT_COLUMNS = (
    ReportColumn("client_name", "Клиент"),
    ReportColumn("inn", "ИНН"),
    ReportColumn("sku_count", "SKU", "number"),
    ReportColumn("stock_qty", "Остаток, шт", "number"),
    ReportColumn("available_qty", "Доступно, шт", "number"),
    ReportColumn("active_operations", "Активные операции", "number"),
    ReportColumn("charges_total", "Начислено, ₽", "number"),
    ReportColumn("debt_total", "Задолженность, ₽", "number"),
)
OPERATION_COLUMNS = (
    ReportColumn("operation_id", "Операция"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("operation_type", "Тип операции"),
    ReportColumn("status", "Статус"),
    ReportColumn("planned_qty", "План", "number"),
    ReportColumn("done_qty", "Выполнено", "number"),
    ReportColumn("progress_percent", "Прогресс, %", "number"),
    ReportColumn("source", "Откуда"),
    ReportColumn("destination", "Куда"),
    ReportColumn("responsible", "Ответственный"),
    ReportColumn("created_at", "Создано", "datetime"),
    ReportColumn("updated_at", "Обновлено", "datetime"),
)
DEBT_COLUMNS = (
    ReportColumn("client_name", "Клиент"),
    ReportColumn("invoice_count", "Счетов", "number"),
    ReportColumn("total_amount", "Выставлено, ₽", "number"),
    ReportColumn("paid_amount", "Оплачено, ₽", "number"),
    ReportColumn("debt_amount", "Долг, ₽", "number"),
    ReportColumn("overdue_amount", "Просрочено, ₽", "number"),
    ReportColumn("nearest_due_date", "Ближайший срок", "date"),
)
NOMENCLATURE_COLUMNS = (
    ReportColumn("client_name", "Клиент"),
    ReportColumn("sku", "Артикул / SKU"),
    ReportColumn("product_name", "Наименование"),
    ReportColumn("barcode", "Основной штрихкод"),
    ReportColumn("barcode_count", "Штрихкодов", "number"),
    ReportColumn("category", "Категория"),
    ReportColumn("brand", "Бренд"),
    ReportColumn("weight_kg", "Вес, кг", "number"),
    ReportColumn("dimensions", "Габариты, мм"),
    ReportColumn("updated_at", "Обновлено", "datetime"),
)
CELL_COLUMNS = (
    ReportColumn("warehouse_name", "Склад"),
    ReportColumn("zone", "Зона"),
    ReportColumn("cell", "Ячейка"),
    ReportColumn("location_type", "Назначение"),
    ReportColumn("capacity", "Вместимость мест", "number"),
    ReportColumn("containers", "Тары размещено", "number"),
    ReportColumn("stock_qty", "Товаров, шт", "number"),
    ReportColumn("status", "Статус"),
    ReportColumn("updated_at", "Обновлено", "datetime"),
)
EMPLOYEE_COLUMNS = (
    ReportColumn("responsible", "Сотрудник"),
    ReportColumn("role", "Роль"),
    ReportColumn("task_count", "Заданий", "number"),
    ReportColumn("completed_count", "Выполнено", "number"),
    ReportColumn("failed_count", "Ошибок", "number"),
    ReportColumn("planned_qty", "План, шт", "number"),
    ReportColumn("done_qty", "Выполнено, шт", "number"),
    ReportColumn("completion_percent", "Выполнение, %", "number"),
    ReportColumn("average_minutes", "Среднее время, мин", "number"),
)
TASK_COLUMNS = (
    ReportColumn("task_id", "Задание"),
    ReportColumn("operation_id", "Операция"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("task_type", "Тип задания"),
    ReportColumn("status", "Статус"),
    ReportColumn("responsible", "Исполнитель"),
    ReportColumn("planned_qty", "План", "number"),
    ReportColumn("done_qty", "Выполнено", "number"),
    ReportColumn("age_hours", "Возраст, ч", "number"),
    ReportColumn("created_at", "Создано", "datetime"),
    ReportColumn("updated_at", "Обновлено", "datetime"),
)
CHARGE_COLUMNS = (
    ReportColumn("group_name", "Клиент / услуга"),
    ReportColumn("charge_count", "Начислений", "number"),
    ReportColumn("quantity", "Количество", "number"),
    ReportColumn("amount", "Без НДС, ₽", "number"),
    ReportColumn("vat_amount", "НДС, ₽", "number"),
    ReportColumn("total_amount", "Итого, ₽", "number"),
    ReportColumn("disputed_count", "Спорных", "number"),
    ReportColumn("excluded_count", "Исключено", "number"),
)
STORAGE_COLUMNS = (
    ReportColumn("day", "Дата", "date"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("pallet_count", "Палет", "number"),
    ReportColumn("box_count", "Коробов", "number"),
    ReportColumn("sku_unit_count", "Единиц товара", "number"),
    ReportColumn("physical_volume_m3", "Объём, м³", "number"),
    ReportColumn("amount", "Без НДС, ₽", "number"),
    ReportColumn("vat_amount", "НДС, ₽", "number"),
    ReportColumn("status", "Статус"),
)
INVOICE_COLUMNS = (
    ReportColumn("number", "Счёт"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("invoice_date", "Дата счёта", "date"),
    ReportColumn("due_date", "Срок оплаты", "date"),
    ReportColumn("total_amount", "Сумма, ₽", "number"),
    ReportColumn("paid_amount", "Оплачено, ₽", "number"),
    ReportColumn("debt_amount", "Долг, ₽", "number"),
    ReportColumn("status", "Статус"),
    ReportColumn("overdue_days", "Просрочка, дней", "number"),
)
PAYMENT_COLUMNS = (
    ReportColumn("paid_at", "Дата оплаты", "datetime"),
    ReportColumn("invoice_number", "Счёт"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("amount", "Сумма, ₽", "number"),
    ReportColumn("status", "Статус"),
    ReportColumn("source", "Источник"),
    ReportColumn("comment", "Комментарий"),
)
CORRECTION_COLUMNS = (
    ReportColumn("performed_at", "Дата", "datetime"),
    ReportColumn("client_name", "Клиент"),
    ReportColumn("service_name", "Услуга"),
    ReportColumn("amount", "Сумма, ₽", "number"),
    ReportColumn("correction_type", "Тип изменения"),
    ReportColumn("reason", "Основание / комментарий"),
    ReportColumn("responsible", "Автор"),
)

CLIENT_REPORTS = (
    _live_report("client-summary", "clients", "Сводка по клиентам", "Остатки, активные операции, начисления и задолженность по каждому клиенту.", ("клиент", "сводка", "долг"), CLIENT_ANALYTICS_FILTERS, CLIENT_COLUMNS),
    _product_alias("clients", "client-stock", "product-stock", "Остатки по клиентам", "Фактические, доступные и зарезервированные остатки клиентов.", ("клиент", "остатки")),
    _product_alias("clients", "client-movement", "product-movement", "Движение товаров по клиентам", "Приход, расход и перемещения с фильтром по клиенту.", ("клиент", "движение")),
    _live_report("client-operations", "clients", "Операции по клиентам", "Складские операции клиентов, их прогресс и текущий статус.", ("клиент", "операции", "статус"), OPERATION_FILTERS, OPERATION_COLUMNS),
    _live_report("client-debt", "clients", "Задолженность клиентов", "Выставленные счета, оплаты и просроченная задолженность.", ("клиент", "долг", "счета"), CLIENT_ANALYTICS_FILTERS, DEBT_COLUMNS),
    _product_alias("clients", "client-slow-stock", "idle-products", "Неликвидные товары по клиентам", "Товары клиентов с остатком, по которым долго не было движения.", ("клиент", "неликвид")),
    _product_alias("clients", "client-discrepancies", "product-income", "Расхождения при приёмке по клиентам", "Принятое количество, недостачи, излишки и брак.", ("клиент", "расхождения", "приёмка")),
)

NOMENCLATURE_REPORTS = (
    _live_report("catalog", "nomenclature", "Справочник номенклатуры", "Активные карточки SKU с основным штрихкодом и весогабаритными данными.", ("sku", "артикул", "карточки"), NOMENCLATURE_FILTERS, NOMENCLATURE_COLUMNS),
    _product_alias("nomenclature", "sku-stock", "product-stock", "Остатки по SKU", "Остатки в разрезе SKU, артикулов и штрихкодов.", ("sku", "остатки")),
    _product_alias("nomenclature", "sku-movement", "product-movement", "Движение по SKU", "История складских событий по SKU и штрихкодам.", ("sku", "движение")),
    _live_report("barcodes", "nomenclature", "Штрихкоды", "Основные и дополнительные штрихкоды по карточкам SKU.", ("штрихкод", "barcode"), NOMENCLATURE_FILTERS, NOMENCLATURE_COLUMNS),
    _product_alias("nomenclature", "expiration-dates", "expiration-dates", "Сроки годности", "Партии и товары по срокам годности и группам риска.", ("срок годности", "партии")),
    _product_alias("nomenclature", "idle-nomenclature", "idle-products", "Номенклатура без движения", "SKU с остатком без движения за выбранный период.", ("sku", "без движения")),
    _live_report("duplicates", "nomenclature", "Возможные дубли номенклатуры", "Активные карточки одного клиента с совпадающим нормализованным названием.", ("дубли", "карточки"), NOMENCLATURE_FILTERS, NOMENCLATURE_COLUMNS),
    _product_alias("nomenclature", "weight-volume", "product-box-flow", "Вес и объём товарных мест", "Вес, объём и габариты коробов и товарных мест.", ("вес", "объём", "габариты")),
    _live_report("without-barcode", "nomenclature", "Номенклатура без штрихкода", "Активные SKU, для которых не указан ни один штрихкод.", ("без штрихкода",), NOMENCLATURE_FILTERS, NOMENCLATURE_COLUMNS),
    _live_report("without-dimensions", "nomenclature", "Номенклатура без габаритов", "SKU без веса, длины, ширины или высоты.", ("без габаритов", "вес"), NOMENCLATURE_FILTERS, NOMENCLATURE_COLUMNS),
)

WAREHOUSE_REPORTS = (
    _product_alias("warehouse", "current-stock", "product-stock", "Остатки на текущую дату", "Фактические, доступные, зарезервированные и заблокированные остатки.", ("остатки", "склад"), direct_url="/team-manager/inventory/"),
    _product_alias("warehouse", "stock-by-warehouse", "product-stock", "Остатки по складам", "Остатки с разбивкой по складам и зонам.", ("остатки", "склады")),
    _product_alias("warehouse", "stock-by-zone", "product-stock", "Остатки по зонам", "Остатки с детализацией по складским зонам.", ("остатки", "зоны")),
    _product_alias("warehouse", "stock-by-cell", "product-cells", "Остатки по ячейкам", "Размещение товаров по адресам хранения.", ("остатки", "ячейки")),
    _product_alias("warehouse", "stock-by-client", "product-stock", "Остатки по клиентам", "Текущие складские остатки клиентов.", ("остатки", "клиенты")),
    _product_alias("warehouse", "stock-by-product", "product-stock", "Остатки по товарам", "Текущие остатки в разрезе SKU.", ("остатки", "товары")),
    _product_alias("warehouse", "stock-movement", "product-movement", "Движение товаров", "Приход, расход, перемещения, резервы и корректировки.", ("движение", "операции")),
    _product_alias("warehouse", "warehouse-load", "product-box-flow", "Загрузка склада", "Товарные места, их объём, вес и текущее состояние.", ("загрузка", "склад")),
    _live_report("empty-cells", "warehouse", "Пустые ячейки", "Активные адреса хранения без размещённой тары и товарного остатка.", ("пустые", "ячейки"), LOCATION_FILTERS, CELL_COLUMNS),
    _live_report("blocked-cells", "warehouse", "Недоступные ячейки", "Отключённые и скрытые адреса, недоступные для складских операций.", ("заблокированные", "ячейки"), LOCATION_FILTERS, CELL_COLUMNS),
    _product_alias("warehouse", "transfers", "product-transfers", "Перемещения", "Перемещения между зонами, ячейками и складами.", ("перемещения",)),
    _product_alias("warehouse", "idle-stock", "idle-products", "Товары без движения", "Положительные остатки без движения за выбранный период.", ("без движения", "неликвид")),
    _product_alias("warehouse", "expiration-dates", "expiration-dates", "Сроки годности", "Товары и партии с истекающим сроком годности.", ("срок годности",)),
    _product_alias("warehouse", "reserves", "product-reserves", "Резервы товаров", "Активные, использованные и снятые резервы.", ("резервы",)),
    _product_alias("warehouse", "warehouse-operation-history", "product-movement", "История складских операций", "Хронологический журнал складских событий.", ("история", "операции")),
)

OPERATIONS_REPORTS = (
    _live_report("employee-productivity", "operations", "Производительность сотрудников", "Количество, объём и среднее время выполнения складских заданий.", ("сотрудник", "производительность"), OPERATION_FILTERS, EMPLOYEE_COLUMNS),
    _live_report("employee-quality", "operations", "Качество работы сотрудников", "Доля завершённых заданий и количество ошибок по исполнителям.", ("качество", "ошибки"), OPERATION_FILTERS, EMPLOYEE_COLUMNS),
    _live_report("operation-sla", "operations", "SLA операций", "Возраст, прогресс и длительность складских операций.", ("sla", "срок"), OPERATION_FILTERS, OPERATION_COLUMNS),
    _live_report("overdue-operations", "operations", "Операции старше суток", "Незавершённые складские операции, созданные более 24 часов назад.", ("просрочка", "операции"), OPERATION_FILTERS, OPERATION_COLUMNS),
    _live_report("unfinished-operations", "operations", "Незавершённые операции", "Все операции в очереди, плане, работе, частичном или заблокированном статусе.", ("незавершённые",), OPERATION_FILTERS, OPERATION_COLUMNS),
    _live_report("stale-operations", "operations", "Операции без обновления", "Незавершённые операции, которые не менялись более 12 часов.", ("зависло", "без движения"), OPERATION_FILTERS, OPERATION_COLUMNS),
    _live_report("warehouse-task-queue", "operations", "Очередь складских заданий", "Текущие задания склада, исполнители, возраст и прогресс.", ("очередь", "задания"), OPERATION_FILTERS, TASK_COLUMNS),
)

FINANCE_REPORTS = (
    _live_report("charges-by-client", "finance", "Начисления по клиентам", "Количество услуг и суммы начислений по каждому клиенту.", ("начисления", "клиент"), FINANCE_FILTERS, CHARGE_COLUMNS),
    _live_report("charges-by-service", "finance", "Начисления по услугам", "Объём и сумма начислений по видам услуг.", ("начисления", "услуги"), FINANCE_FILTERS, CHARGE_COLUMNS),
    _live_report("storage-cost", "finance", "Стоимость хранения", "Дневные объёмы хранения и начисленная стоимость.", ("хранение", "стоимость"), FINANCE_FILTERS, STORAGE_COLUMNS),
    _live_report("invoices", "finance", "Счета", "Счета клиентов, сроки, оплаты, задолженность и статусы.", ("счета",), FINANCE_FILTERS, INVOICE_COLUMNS),
    _live_report("payments", "finance", "Оплаты", "Зарегистрированные оплаты с привязкой к счетам.", ("оплаты",), FINANCE_FILTERS, PAYMENT_COLUMNS),
    _live_report("client-debt", "finance", "Задолженность клиентов", "Текущая и просроченная задолженность по клиентам.", ("задолженность",), FINANCE_FILTERS, DEBT_COLUMNS),
    _live_report("overdue-debt", "finance", "Просроченная задолженность", "Неоплаченные счета с истёкшим сроком оплаты.", ("просроченная", "задолженность"), FINANCE_FILTERS, INVOICE_COLUMNS),
    _live_report("charge-corrections", "finance", "Корректировки начислений", "Ручные изменения, исключения и корректирующие строки начислений.", ("корректировки",), FINANCE_FILTERS, CORRECTION_COLUMNS),
)


SECTIONS = (
    ReportSection(
        "clients",
        "Отчёты по клиентам",
        "Заявки, товары, услуги, остатки, операции и взаиморасчёты по клиентам.",
        "◎",
        CLIENT_REPORTS,
        MANAGER_REPORT_ROLES,
    ),
    ReportSection(
        "products",
        "Отчёты по товарам",
        "Остатки, движение, востребованность и оборачиваемость товаров.",
        "◈",
        PRODUCT_REPORTS,
        MANAGER_REPORT_ROLES,
    ),
    ReportSection(
        "nomenclature",
        "Отчёты по номенклатуре",
        "Аналитика на уровне SKU, артикулов, штрихкодов, партий и серий.",
        "▧",
        NOMENCLATURE_REPORTS,
        MANAGER_REPORT_ROLES,
    ),
    ReportSection(
        "warehouse",
        "Складские отчёты",
        "Остатки, движение, адресное хранение, приёмка, отгрузка и инвентаризация.",
        "▤",
        WAREHOUSE_REPORTS,
        MANAGER_REPORT_ROLES,
    ),
    ReportSection(
        "operations",
        "Операционные отчёты",
        "Производительность сотрудников, скорость и качество выполнения операций.",
        "☑",
        OPERATIONS_REPORTS,
        MANAGER_REPORT_ROLES,
    ),
    ReportSection(
        "finance",
        "Финансовые отчёты",
        "Начисления, стоимость услуг, хранение, счета, оплаты и задолженность.",
        "₽",
        FINANCE_REPORTS,
        FINANCE_REPORT_ROLES,
    ),
)

SECTION_BY_KEY = {section.key: section for section in SECTIONS}
REPORT_BY_PATH = {
    (report.section, report.code): report
    for section in SECTIONS
    for report in section.reports
}


def role_can_open_section(role: str | None, section: ReportSection) -> bool:
    return not section.roles or role in section.roles


def sections_for_role(role: str | None) -> tuple[ReportSection, ...]:
    return tuple(section for section in SECTIONS if role_can_open_section(role, section))


def get_section(key: str, role: str | None = None) -> ReportSection | None:
    section = SECTION_BY_KEY.get(key)
    if section is None or not role_can_open_section(role, section):
        return None
    return section


def get_report(section_key: str, report_code: str, role: str | None = None) -> ReportDefinition | None:
    section = get_section(section_key, role)
    if section is None:
        return None
    report = REPORT_BY_PATH.get((section_key, report_code))
    return report if report in section.reports else None


def search_reports(query: str, role: str | None = None, section_key: str | None = None) -> tuple[ReportDefinition, ...]:
    query = (query or "").strip().lower()
    sections = sections_for_role(role)
    if section_key:
        sections = tuple(section for section in sections if section.key == section_key)
    reports = [report for section in sections for report in section.reports]
    if not query:
        return tuple(reports)
    result = []
    for report in reports:
        section = SECTION_BY_KEY.get(report.section)
        haystack = " ".join(
            [
                report.title,
                report.description,
                " ".join(report.keywords),
                section.title if section else "",
                section.description if section else "",
            ]
        ).lower()
        if query in haystack:
            result.append(report)
    return tuple(result)


def active_filters_from_params(params) -> list[dict[str, str]]:
    labels = {
        "period": "Период",
        "date": "Дата",
        "date_from": "Дата с",
        "date_to": "Дата по",
        "client_id": "Клиент",
        "warehouse_id": "Склад",
        "product": "Товар",
        "category": "Категория",
        "article": "Артикул",
        "operation_type": "Тип операции",
        "legal_entity": "Юр. лицо",
        "responsible": "Ответственный",
        "zone": "Зона",
        "cell": "Ячейка",
        "sku": "SKU / артикул",
        "barcode": "Штрихкод",
        "batch": "Партия",
        "status": "Статус",
        "stock_type": "Тип остатка",
        "has_reserve": "Наличие резерва",
        "only_positive": "Только положительный",
        "only_available": "Только доступный",
        "only_zero": "Только нулевые",
        "idle_days": "Дней без движения",
        "page_size": "Строк на странице",
        "sort": "Сортировка",
    }
    active: list[dict[str, str]] = []
    for key, label in labels.items():
        value = params.get(key)
        if value not in (None, "", "all", "0"):
            active.append({"code": key, "label": label, "value": str(value)})
    return active


def global_filter_query(params) -> str:
    allowed = ("period", "date", "date_from", "date_to", "client_id", "warehouse_id", "operation_type", "legal_entity")
    payload = {key: params.get(key) for key in allowed if params.get(key)}
    return urlencode(payload)


def report_to_dict(report: ReportDefinition, *, is_favorite: bool = False, last_generated_at=None) -> dict:
    section = SECTION_BY_KEY[report.section]
    return {
        "code": report.code,
        "section": report.section,
        "section_title": section.title,
        "title": report.title,
        "description": report.description,
        "keywords": report.keywords,
        "filters": report.filters,
        "columns": report.columns,
        "export_formats": report.export_formats,
        "status": report.status,
        "url": report.url,
        "is_favorite": is_favorite,
        "last_generated_at": last_generated_at,
    }
