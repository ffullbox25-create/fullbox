from __future__ import annotations

from dataclasses import dataclass

from .models import BillingService


@dataclass(frozen=True)
class ServiceDefinition:
    code: str
    name: str
    unit: str = "усл."
    vat_rate: str = "20"


SERVICE_CATALOG: tuple[ServiceDefinition, ...] = (
    ServiceDefinition("receiving_goods", "Приёмка товара на склад", "усл."),
    ServiceDefinition("receiving_box", "Приёмка коробов", "кор."),
    ServiceDefinition("receiving_pallet", "Приёмка палет", "пал."),
    ServiceDefinition("receiving_recount", "Пересчёт при приёмке", "шт."),
    ServiceDefinition("receiving_placement", "Размещение на складе", "усл."),
    ServiceDefinition("processing_marking", "Маркировка", "шт."),
    ServiceDefinition("processing_packaging", "Упаковка", "шт."),
    ServiceDefinition("processing_remarking", "Перемаркировка", "шт."),
    ServiceDefinition("processing_stickering", "Стикеровка", "шт."),
    ServiceDefinition("processing_kitting", "Комплектование", "шт."),
    ServiceDefinition("shipping_pick_items", "Подбор товаров", "шт."),
    ServiceDefinition("shipping_form_pallet", "Формирование палет", "пал."),
    ServiceDefinition("shipping_release_pallet", "Выдача палет", "пал."),
    ServiceDefinition("shipping_dispatch", "Отгрузка", "усл."),
    ServiceDefinition("shipping_delivery_pallet", "Доставка палет", "пал."),
    ServiceDefinition("logistics_external_trip", "Внешний транспортный рейс", "рейс"),
    ServiceDefinition("movement_goods", "Перемещение товара", "шт."),
    ServiceDefinition("movement_pallet", "Перемещение палет", "пал."),
    ServiceDefinition("storage_pallet_day", "Хранение палеты/день", "пал."),
    ServiceDefinition("storage_pallet_week", "Хранение палеты/неделя", "пал."),
    ServiceDefinition("storage_pallet_month", "Хранение палеты/месяц", "пал."),
    ServiceDefinition("storage_liter_day", "Хранение, литр/сутки", "л"),
    ServiceDefinition("storage_liter_week", "Хранение, литр/неделя", "л"),
    ServiceDefinition("storage_liter_month", "Хранение, литр/месяц", "л"),
    ServiceDefinition("storage_m3_day", "Хранение, м³/сутки", "м3"),
    ServiceDefinition("storage_m3_week", "Хранение, м³/неделя", "м3"),
    ServiceDefinition("storage_m3_month", "Хранение, м³/месяц", "м3"),

    ServiceDefinition("extra_warehouse_operation", "Прочая складская услуга", "усл."),
)


def ensure_service_catalog() -> dict[str, BillingService]:
    services: dict[str, BillingService] = {}
    for item in SERVICE_CATALOG:
        service, _created = BillingService.objects.update_or_create(
            code=item.code,
            defaults={
                "name": item.name,
                "unit": item.unit,
                "vat_rate": item.vat_rate,
                "is_active": True,
            },
        )
        services[item.code] = service
    return services
