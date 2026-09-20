from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import BillingService, StandardServicePrice


@dataclass(frozen=True)
class StandardPriceDefinition:
    section_code: str
    section_name: str
    line_no: int
    code: str
    name: str
    unit: str
    price: Decimal | None
    note: str = ""


STANDARD_PRICE_CATALOG: tuple[StandardPriceDefinition, ...] = (
    StandardPriceDefinition("01", "Въезд и приёмка автомобилей", 1, "vehicle_entry_truck", "Въезд на территорию фулфилмента (грузовой автомобиль)", "шт", Decimal("100")),
    StandardPriceDefinition("01", "Въезд и приёмка автомобилей", 2, "vehicle_entry_car", "Въезд на территорию фулфилмента (легковой автомобиль)", "шт", Decimal("50")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 1, "receiving_pallet_mechanized_upto_500kg", "Механизированная приёмка палеты до 500 кг, высота до 1,8 м", "шт", Decimal("250")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 2, "receiving_pallet_mechanized_500_1000kg", "Механизированная приёмка палеты от 500 до 1000 кг", "шт", Decimal("400")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 3, "receiving_pallet_mechanized_over_1000kg", "Механизированная приёмка палеты более 1000 кг", "шт", Decimal("1500")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 4, "receiving_unload_manual_1_15kg", "Ручная выгрузка товара на склад, от 1 до 15 кг", "шт", Decimal("20")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 5, "receiving_unload_manual_15_30kg", "Ручная выгрузка товара на склад, от 15 до 30 кг", "шт", Decimal("30")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 6, "receiving_unload_manual_30_50kg", "Ручная выгрузка товара на склад, от 30 до 50 кг", "шт", Decimal("40")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 7, "receiving_form_pallet_loose", "Формирование палеты при приёмке (товар привезён россыпью)", "шт", Decimal("200")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 8, "receiving_remove_crate_pallet", "Снятие обрешётки (палета) с утилизацией отходов", "шт", Decimal("1500")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 9, "receiving_remove_crate_box", "Снятие обрешётки (короб) с утилизацией отходов", "шт", Decimal("800")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 10, "receiving_remove_bag_packaging", "Снятие упаковки (мешок) с утилизацией отходов", "шт", Decimal("100")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 11, "receiving_goods", "Приёмка товара (пересчёт общего количества)", "шт", Decimal("6")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 12, "receiving_distribute_destinations", "Распределение товара по направлениям", "шт", Decimal("6")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 13, "receiving_identify_photo", "Идентификация товара по фото (нет артикула/ШК)", "шт", Decimal("30")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 14, "receiving_goods_1_15kg", "Приёмка товара на склад, от 1 до 15 кг", "шт", Decimal("20")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 15, "receiving_goods_15_30kg", "Приёмка товара на склад, от 15 до 30 кг", "шт", Decimal("30")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 16, "receiving_goods_30_50kg", "Приёмка товара на склад, от 30 до 50 кг", "шт", Decimal("40")),
    StandardPriceDefinition("02", "Приёмка и выгрузка товара", 17, "receiving_pallet", "Приёмка товара на склад, палетная", "шт", Decimal("100")),
    StandardPriceDefinition("03", "Маркировка", 1, "processing_marking_barcode_75x120", "Маркировка (ШК 75×120)", "шт", Decimal("15")),
    StandardPriceDefinition("03", "Маркировка", 2, "processing_marking_barcode_58x40", "Маркировка (ШК 58×40)", "шт", Decimal("7")),
    StandardPriceDefinition("03", "Маркировка", 3, "processing_marking_chz_58x40", "Маркировка (ЧЗ 58×40)", "шт", Decimal("8")),
    StandardPriceDefinition("04", "Упаковка", 1, "processing_packaging_vacuum_bag", "Упаковка товара в вакуумный пакет, с учётом пакета", "шт", Decimal("55")),
    StandardPriceDefinition("04", "Упаковка", 2, "processing_packaging_thermal_bag", "Упаковка в термопакет", "шт", Decimal("10"), "от 10 ₽"),
    StandardPriceDefinition("04", "Упаковка", 3, "processing_packaging_bubble_wrap", "Упаковка в бабл-плёнку", "шт", Decimal("10"), "от 10 ₽"),
    StandardPriceDefinition("04", "Упаковка", 4, "processing_packaging", "Упаковка товара в пакет / короб, без учёта материала", "шт", Decimal("6")),
    StandardPriceDefinition("05", "Комплектация наборов", 1, "processing_kitting_upto_3", "Комплектация набора, до 3 единиц, за ед.", "шт", Decimal("6")),
    StandardPriceDefinition("05", "Комплектация наборов", 2, "processing_kitting_upto_5", "Комплектация набора, до 5 единиц, за ед.", "шт", Decimal("5")),
    StandardPriceDefinition("05", "Комплектация наборов", 3, "processing_kitting_over_5", "Комплектация набора, более 5 единиц, за ед.", "шт", Decimal("3")),
    StandardPriceDefinition("06", "Подготовка и проверка товара", 1, "processing_remove_factory_tag", "Удаление заводской бирки", "шт", Decimal("3")),
    StandardPriceDefinition("06", "Подготовка и проверка товара", 2, "processing_attach_tag_with_holder", "Скрепление бирки на бирко-держатель, с учётом бирки", "шт", Decimal("10")),
    StandardPriceDefinition("06", "Подготовка и проверка товара", 3, "processing_attach_tag_holder_only", "Скрепление бирки на бирко-держатель, без учёта бирки", "шт", Decimal("6")),
    StandardPriceDefinition("06", "Подготовка и проверка товара", 4, "processing_defect_check_detail", "Проверка на брак (детальная)", "шт", Decimal("15"), "от 15 ₽"),
    StandardPriceDefinition("06", "Подготовка и проверка товара", 5, "processing_defect_check_electronics", "Проверка на брак электроники", "шт", Decimal("20"), "от 20 ₽"),
    StandardPriceDefinition("06", "Подготовка и проверка товара", 6, "processing_insert_advertising", "Дополнительные вложения", "шт", Decimal("3")),
    StandardPriceDefinition("06", "Подготовка и проверка товара", 7, "processing_measure_clothes", "Замер товаров одежды (1 размер)", "шт", Decimal("30")),
    StandardPriceDefinition("06", "Подготовка и проверка товара", 8, "processing_measure_box", "Замер габаритов коробки", "шт", Decimal("20")),
    StandardPriceDefinition("06", "Подготовка и проверка товара", 9, "processing_gift_box_packaging", "Сборка/упаковка подарочного короба, за ед.", "шт", Decimal("15")),
    StandardPriceDefinition("07", "Формирование коробов и палет", 1, "processing_assemble_by_destination", "Сборка товара по направлениям при обработке, за 1 ед.", "шт", Decimal("8")),
    StandardPriceDefinition("07", "Формирование коробов и палет", 2, "processing_form_transport_box_upto_10", "Формирование транспортировочного короба, до 10 ед.", "шт", Decimal("4")),
    StandardPriceDefinition("07", "Формирование коробов и палет", 3, "processing_form_transport_box_upto_50", "Формирование транспортировочного короба, до 50 ед.", "шт", Decimal("3")),
    StandardPriceDefinition("07", "Формирование коробов и палет", 4, "processing_form_transport_box_upto_100", "Формирование транспортировочного короба, до 100 ед.", "шт", Decimal("2")),
    StandardPriceDefinition("07", "Формирование коробов и палет", 5, "processing_form_transport_box_over_100", "Формирование транспортировочного короба, более 100 ед.", "шт", Decimal("1.5")),
    StandardPriceDefinition("07", "Формирование коробов и палет", 6, "processing_provide_box_60x40x40", "Предоставление короба 60×40×40", "шт", Decimal("100")),
    StandardPriceDefinition("07", "Формирование коробов и палет", 7, "processing_provide_box_60x40x25", "Предоставление короба 60×40×25", "шт", Decimal("80")),
    StandardPriceDefinition("08", "Сборка заказов с хранения", 1, "shipping_pick_storage_item", "Сборка товара по листу подбора с хранения, 1 ед.", "шт", Decimal("15")),
    StandardPriceDefinition("08", "Сборка заказов с хранения", 2, "shipping_pick_box_1_15kg", "Сборка короба по листу подбора, от 1 до 15 кг", "шт", Decimal("35")),
    StandardPriceDefinition("08", "Сборка заказов с хранения", 3, "shipping_pick_box_15_30kg", "Сборка короба по листу подбора, от 15 до 30 кг", "шт", Decimal("45")),
    StandardPriceDefinition("08", "Сборка заказов с хранения", 4, "shipping_pick_box_30_50kg", "Сборка короба по листу подбора, от 30 до 50 кг", "шт", Decimal("55")),
    StandardPriceDefinition("08", "Сборка заказов с хранения", 5, "shipping_form_pallet", "Сборка палеты по листу подбора с хранения", "шт", Decimal("100")),
    StandardPriceDefinition("08", "Сборка заказов с хранения", 6, "shipping_form_euro_pallet", "Сборка евро-палеты", "шт", Decimal("850")),
    StandardPriceDefinition("08", "Сборка заказов с хранения", 7, "shipping_form_mono_pallet", "Сборка евро-палеты, моно", "шт", Decimal("1050")),
    StandardPriceDefinition("09", "Печать этикеток и документов", 1, "print_box_label_75x120", "Печать этикетки ШК короба, 75×120 мм", "шт", Decimal("15")),
    StandardPriceDefinition("09", "Печать этикеток и документов", 2, "print_supply_label_75x120", "Печать этикетки ШК поставки, 75×120 мм", "шт", Decimal("15")),
    StandardPriceDefinition("09", "Печать этикеток и документов", 3, "print_packing_list_label_75x120", "Печать этикетки упаковочного листа, 75×120 мм", "шт", Decimal("15")),
    StandardPriceDefinition("09", "Печать этикеток и документов", 4, "print_documents_set", "Печать комплекта сопроводительных документов (акт, УПД)", "шт", Decimal("100")),
    StandardPriceDefinition("10", "Палетирование и погрузка", 1, "shipping_stretch_pallet_2_layers", "Стрейчевание палеты, 2 слоя", "шт", Decimal("250")),
    StandardPriceDefinition("10", "Палетирование и погрузка", 2, "shipping_stretch_mono_pallet_3_layers", "Стрейчевание монопалеты, 3 слоя", "шт", Decimal("360")),
    StandardPriceDefinition("10", "Палетирование и погрузка", 3, "shipping_load_boxes_upto_25kg", "Погрузка в машину коробами, до 25 кг", "шт", Decimal("60")),
    StandardPriceDefinition("10", "Палетирование и погрузка", 4, "shipping_load_pallet_upto_500kg", "Погрузка в машину палет, до 500 кг", "шт", Decimal("250")),
    StandardPriceDefinition("10", "Палетирование и погрузка", 5, "shipping_load_pallet_500_1000kg", "Погрузка в машину палет, от 500 до 1000 кг", "шт", Decimal("400")),
    StandardPriceDefinition("10", "Палетирование и погрузка", 6, "shipping_load_pallet_over_1000kg", "Погрузка в машину палет, более 1000 кг", "шт", Decimal("1000")),
    StandardPriceDefinition("11", "Логистика и забор товара", 1, "logistics_pickup_goods_market", "Забор товара (Южные Ворота, ТЯК Москва и т.д.)", "шт", Decimal("5500")),
    StandardPriceDefinition("11", "Логистика и забор товара", 2, "logistics_create_wb_ozon_supplies", "Оформление поставок на WB и OZON", "шт", Decimal("700")),
    StandardPriceDefinition("11", "Логистика и забор товара", 3, "logistics_return_pickup_item", "Забор возвратов с пункта выдачи, за ед.", "шт", Decimal("50"), "мин. заказ 500 ₽"),
    StandardPriceDefinition("12", "Работа с возвратами", 1, "returns_processing", "Обработка возврата", "шт", Decimal("80")),
    StandardPriceDefinition("12", "Работа с возвратами", 2, "returns_video_recording", "Видеофиксация товара", "шт", Decimal("50")),
    StandardPriceDefinition("13", "Хранение", 1, "storage_pallet_day", "Хранение палеты, сутки (высота до 1,8 м)", "шт", Decimal("100")),
    StandardPriceDefinition("13", "Хранение", 2, "storage_pallet_day_10plus", "Хранение от 10 палет, сутки", "шт", Decimal("70")),
    StandardPriceDefinition("13", "Хранение", 3, "storage_pallet_day_100plus", "Хранение от 100 палет, сутки", "шт", Decimal("50")),
    StandardPriceDefinition("13", "Хранение", 4, "storage_pallet_day_150plus", "Хранение свыше 150 палет, длительное", "шт", Decimal("45")),
    StandardPriceDefinition("13", "Хранение", 5, "storage_liter_day", "Хранение, 1 литр в сутки", "л", Decimal("0.08")),
    StandardPriceDefinition("13", "Хранение", 6, "storage_liter_week", "Хранение, 1 литр в неделю", "л", Decimal("0.50")),
    StandardPriceDefinition("13", "Хранение", 7, "storage_liter_month", "Хранение, 1 литр в месяц", "л", Decimal("2.00")),
    StandardPriceDefinition("13", "Хранение", 8, "storage_m3_day", "Хранение, 1 м³ в сутки", "м3", Decimal("80")),
    StandardPriceDefinition("13", "Хранение", 9, "storage_m3_week", "Хранение, 1 м³ в неделю", "м3", Decimal("500")),
    StandardPriceDefinition("13", "Хранение", 10, "storage_m3_month", "Хранение, 1 м³ в месяц", "м3", Decimal("2000")),
    StandardPriceDefinition("13", "Хранение", 11, "storage_pallet_week", "Хранение палеты, неделя", "пал.", Decimal("600")),
    StandardPriceDefinition("13", "Хранение", 12, "storage_pallet_month", "Хранение палеты, месяц", "пал.", Decimal("2500")),
)


SECTION_TO_CATEGORY = {
    "01": ("receiving", "Приемка", 10),
    "02": ("receiving", "Приемка", 10),
    "03": ("marking", "Маркировка", 30),
    "04": ("packing", "Упаковка", 40),
    "05": ("processing", "Обработка", 20),
    "06": ("processing", "Обработка", 20),
    "07": ("processing", "Обработка", 20),
    "08": ("shipping", "Отгрузка", 60),
    "09": ("marking", "Маркировка", 30),
    "10": ("shipping", "Отгрузка", 60),
    "11": ("logistics", "Логистика", 70),
    "12": ("extra", "Дополнительные услуги", 80),
    "13": ("storage", "Хранение", 50),
}


def _category_for_section(section_code: str):
    from .models import TariffCategory

    code, name, order = SECTION_TO_CATEGORY.get(section_code, ("other", "Прочее", 100))
    cat, _ = TariffCategory.objects.get_or_create(
        code=code,
        defaults={"name": name, "sort_order": order, "is_active": True},
    )
    return cat


def _billing_service_column_names() -> set[str]:
    from django.db import connection

    with connection.cursor() as cursor:
        return {col.name for col in connection.introspection.get_table_description(cursor, "billing_billingservice")}


def seed_standard_prices(service_model=None, price_model=None, category_model=None) -> dict[str, int]:
    """Синхронизирует BillingService + StandardServicePrice с прайсом FullBox.

    При вызове из старых миграций передавайте historical models через service_model/price_model,
    иначе live ORM (с полями category и т.п.) ломает schema на шаге 0004.
    """
    from .models import BillingService as LiveService
    from .models import StandardServicePrice as LivePrice

    BillingService = service_model or LiveService
    StandardServicePrice = price_model or LivePrice
    created = 0
    updated = 0
    columns = _billing_service_column_names()
    model_field_names = {f.name for f in BillingService._meta.local_concrete_fields}
    for item in STANDARD_PRICE_CATALOG:
        sort_order = int(item.section_code) * 1000 + item.line_no * 10
        defaults = {
            "name": item.name,
            "unit": item.unit,
            "vat_rate": "20",
            "is_active": True,
        }
        if "category" in model_field_names and "category_id" in columns:
            if category_model is not None:
                code, name, order = SECTION_TO_CATEGORY.get(item.section_code, ("other", "Прочее", 100))
                cat, _ = category_model.objects.get_or_create(
                    code=code,
                    defaults={"name": name, "sort_order": order, "is_active": True},
                )
                defaults["category"] = cat
            else:
                defaults["category"] = _category_for_section(item.section_code)
        if "default_price" in model_field_names and "default_price" in columns:
            defaults["default_price"] = item.price
        if "description" in model_field_names and "description" in columns:
            defaults["description"] = item.note or ""
        if "sort_order" in model_field_names and "sort_order" in columns:
            defaults["sort_order"] = sort_order
        if "used_in_billing" in model_field_names and "used_in_billing" in columns:
            defaults["used_in_billing"] = True
        service, service_created = BillingService.objects.update_or_create(
            code=item.code,
            defaults=defaults,
        )
        _price, price_created = StandardServicePrice.objects.update_or_create(
            service=service,
            defaults={
                "section_code": item.section_code,
                "section_name": item.section_name,
                "line_no": item.line_no,
                "name": item.name,
                "unit": item.unit,
                "base_price": item.price,
                "price_note": item.note,
                "is_active": True,
            },
        )
        if service_created or price_created:
            created += 1
        else:
            updated += 1
    return {"created": created, "updated": updated, "total": len(STANDARD_PRICE_CATALOG)}


def standard_price_by_service_id() -> dict[int, StandardServicePrice]:
    return {
        row.service_id: row
        for row in StandardServicePrice.objects.select_related("service").filter(is_active=True)
    }
