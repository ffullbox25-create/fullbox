from __future__ import annotations

from dataclasses import dataclass

from .models import TariffCategory, TariffUnit


@dataclass(frozen=True)
class CategoryDefinition:
    code: str
    name: str
    sort_order: int
    description: str = ""


@dataclass(frozen=True)
class UnitDefinition:
    code: str
    name: str
    short_name: str


TARIFF_CATEGORIES: tuple[CategoryDefinition, ...] = (
    CategoryDefinition("receiving", "Приёмка товара", 10),
    CategoryDefinition("unloading", "Разгрузка", 20),
    CategoryDefinition("processing", "Обработка товара", 30),
    CategoryDefinition("marking", "Маркировка", 40),
    CategoryDefinition("chestny_znak", "Честный знак", 50),
    CategoryDefinition("packaging", "Упаковка", 60),
    CategoryDefinition("materials", "Расходные материалы", 70),
    CategoryDefinition("storage", "Хранение", 80),
    CategoryDefinition("fbo_kitting", "Комплектация FBO", 90),
    CategoryDefinition("fbs_processing", "Обработка FBS", 100),
    CategoryDefinition("shipping", "Отгрузка", 110),
    CategoryDefinition("logistics", "Логистика", 120),
    CategoryDefinition("returns", "Возвраты", 130),
    CategoryDefinition("extra", "Дополнительные услуги", 140),
    CategoryDefinition("individual", "Индивидуальные услуги", 150),
)


TARIFF_UNITS: tuple[UnitDefinition, ...] = (
    UnitDefinition("piece", "Штука", "шт."),
    UnitDefinition("box", "Короб", "кор."),
    UnitDefinition("pallet", "Палета", "пал."),
    UnitDefinition("order", "Заказ", "заказ"),
    UnitDefinition("request", "Заявка", "заявка"),
    UnitDefinition("supply", "Поставка", "поставка"),
    UnitDefinition("shipment", "Отгрузка", "отгр."),
    UnitDefinition("vehicle", "Машина", "маш."),
    UnitDefinition("trip", "Рейс", "рейс"),
    UnitDefinition("kg", "Килограмм", "кг"),
    UnitDefinition("liter", "Литр", "л"),
    UnitDefinition("m3", "Кубический метр", "м³"),
    UnitDefinition("m3_day", "Кубический метр в сутки", "м³/сутки"),
    UnitDefinition("pallet_day", "Палета в сутки", "пал./сутки"),
    UnitDefinition("box_day", "Короб в сутки", "кор./сутки"),
    UnitDefinition("hour", "Час", "час"),
    UnitDefinition("minute", "Минута", "мин."),
    UnitDefinition("sheet", "Лист", "лист"),
    UnitDefinition("label", "Этикетка", "этик."),
    UnitDefinition("marking_code", "Код маркировки", "КМ"),
    UnitDefinition("operation", "Операция", "операция"),
    UnitDefinition("fixed", "Фиксированная стоимость", "фикс."),
)


def seed_tariff_catalog() -> dict[str, int]:
    categories = 0
    units = 0
    for item in TARIFF_CATEGORIES:
        _obj, created = TariffCategory.objects.update_or_create(
            code=item.code,
            defaults={
                "name": item.name,
                "description": item.description,
                "sort_order": item.sort_order,
                "is_active": True,
            },
        )
        categories += int(created)
    for item in TARIFF_UNITS:
        _obj, created = TariffUnit.objects.update_or_create(
            code=item.code,
            defaults={
                "name": item.name,
                "short_name": item.short_name,
                "is_active": True,
            },
        )
        units += int(created)
    return {
        "categories_created": categories,
        "units_created": units,
        "categories_total": len(TARIFF_CATEGORIES),
        "units_total": len(TARIFF_UNITS),
    }
