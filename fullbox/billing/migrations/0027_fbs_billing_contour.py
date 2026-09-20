from django.db import migrations, models


FBS_CATEGORIES = (
    ("fbs_receiving", "FBS · Приемка и перемещение", 101),
    ("fbs_picking", "FBS · Сборка", 102),
    ("fbs_shipping", "FBS · Отгрузка", 103),
    ("fbs_delivery", "FBS · Доставка", 104),
    ("fbs_storage", "FBS · Хранение", 105),
)

FBS_SERVICES = (
    ("fbs_receiving_goods", "FBS · Приемка товара", "шт", "fbs_receiving", 1010),
    ("fbs_movement_box", "FBS · Перемещение коробов", "кор.", "fbs_receiving", 1020),
    ("fbs_pick_item", "FBS · Подбор товара", "шт", "fbs_picking", 1030),
    ("fbs_pick_order", "FBS · Сборка заказа", "заказ", "fbs_picking", 1040),
    ("fbs_shipping_order", "FBS · Отгрузка заказа", "заказ", "fbs_shipping", 1050),
    ("fbs_shipping_box", "FBS · Отгрузка короба", "кор.", "fbs_shipping", 1060),
    ("fbs_delivery", "FBS · Доставка", "рейс", "fbs_delivery", 1070),
    ("fbs_storage_liter_day", "FBS · Хранение, литр/сутки", "л", "fbs_storage", 1080),
    ("fbs_storage_pallet_day", "FBS · Хранение, палето-место/сутки", "пал.", "fbs_storage", 1090),
)


def seed_fbs_catalog(apps, schema_editor):
    BillingService = apps.get_model("billing", "BillingService")
    TariffCategory = apps.get_model("billing", "TariffCategory")
    categories = {}
    for code, name, sort_order in FBS_CATEGORIES:
        category, _ = TariffCategory.objects.update_or_create(
            code=code,
            defaults={"name": name, "sort_order": sort_order, "is_active": True},
        )
        categories[code] = category
    for code, name, unit, category_code, sort_order in FBS_SERVICES:
        BillingService.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "unit": unit,
                "category": categories[category_code],
                "sort_order": sort_order,
                "used_in_billing": True,
                "is_active": True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [("billing", "0026_warehouse_service_fact_unlisted")]

    operations = [
        migrations.AlterField(
            model_name="billingapplication",
            name="application_type",
            field=models.CharField(
                choices=[
                    ("receiving", "Приемка"),
                    ("processing", "Обработка"),
                    ("packing", "Упаковка"),
                    ("shipping", "Отгрузка"),
                    ("logistics", "Логистика"),
                    ("storage", "Хранение"),
                    ("other", "Другая заявка"),
                    ("fbs", "FBS"),
                ],
                max_length=32,
                verbose_name="Тип заявки",
            ),
        ),
        migrations.AddField(
            model_name="clientinvoice",
            name="billing_contour",
            field=models.CharField(
                choices=[("fbo", "FBO / основные услуги"), ("fbs", "FBS")],
                db_index=True,
                default="fbo",
                max_length=16,
                verbose_name="Контур биллинга",
            ),
        ),
        migrations.AlterField(
            model_name="warehouseservicefact",
            name="order_type",
            field=models.CharField(
                choices=[
                    ("receiving", "Приёмка"),
                    ("processing", "Обработка"),
                    ("packing", "Упаковка"),
                    ("shipping", "Отгрузка"),
                    ("fbs", "FBS"),
                ],
                db_index=True,
                max_length=32,
                verbose_name="Тип заявки",
            ),
        ),
        migrations.RunPython(seed_fbs_catalog, migrations.RunPython.noop),
    ]
