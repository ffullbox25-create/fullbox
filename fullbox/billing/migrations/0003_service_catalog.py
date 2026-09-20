# Generated manually for billing service catalog.

from django.db import migrations, models


SERVICE_CATALOG = (
    ("receiving_goods", "Приёмка товара на склад", "усл.", "20"),
    ("receiving_box", "Приёмка коробов", "кор.", "20"),
    ("receiving_pallet", "Приёмка палет", "пал.", "20"),
    ("receiving_recount", "Пересчёт при приёмке", "шт.", "20"),
    ("receiving_placement", "Размещение на складе", "усл.", "20"),
    ("processing_marking", "Маркировка", "шт.", "20"),
    ("processing_packaging", "Упаковка", "шт.", "20"),
    ("processing_remarking", "Перемаркировка", "шт.", "20"),
    ("processing_stickering", "Стикеровка", "шт.", "20"),
    ("processing_kitting", "Комплектование", "шт.", "20"),
    ("shipping_pick_items", "Подбор товаров", "шт.", "20"),
    ("shipping_form_pallet", "Формирование палет", "пал.", "20"),
    ("shipping_release_pallet", "Выдача палет", "пал.", "20"),
    ("shipping_dispatch", "Отгрузка", "усл.", "20"),
    ("shipping_delivery_pallet", "Доставка палет", "пал.", "20"),
    ("movement_goods", "Перемещение товара", "шт.", "20"),
    ("movement_pallet", "Перемещение палет", "пал.", "20"),
    ("storage_pallet_day", "Хранение палеты/день", "пал.", "20"),
    ("extra_warehouse_operation", "Прочая складская услуга", "усл.", "20"),
)


def seed_services(apps, schema_editor):
    BillingService = apps.get_model("billing", "BillingService")
    for code, name, unit, vat_rate in SERVICE_CATALOG:
        BillingService.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "unit": unit,
                "vat_rate": vat_rate,
                "is_active": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0002_storage_day"),
    ]

    operations = [
        migrations.AlterField(
            model_name="applicationcharge",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("manual", "Ручное начисление"),
                    ("client_service_charge", "Начисление ЛК клиента"),
                    ("shipping_transport_note", "Транспортная накладная"),
                    ("storage_day", "Хранение палето-день"),
                    ("warehouse_application", "Складская заявка"),
                ],
                default="manual",
                max_length=64,
                verbose_name="Тип источника",
            ),
        ),
        migrations.RunPython(seed_services, migrations.RunPython.noop),
    ]
