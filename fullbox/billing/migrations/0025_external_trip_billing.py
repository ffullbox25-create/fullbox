from django.db import migrations, models


def seed_external_trip_service(apps, schema_editor):
    BillingService = apps.get_model("billing", "BillingService")
    TariffCategory = apps.get_model("billing", "TariffCategory")
    category, _ = TariffCategory.objects.get_or_create(
        code="logistics",
        defaults={"name": "Логистика", "sort_order": 70, "is_active": True},
    )
    BillingService.objects.update_or_create(
        code="logistics_external_trip",
        defaults={
            "name": "Внешний транспортный рейс",
            "unit": "рейс",
            "vat_rate": "20",
            "category": category,
            "default_price": None,
            "description": "Перевозка груза из точки А в точку Б без внутренней заявки на отгрузку.",
            "sort_order": 11030,
            "used_in_billing": True,
            "is_active": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("billing", "0024_logistics_tariff_charge_snapshot")]

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
                ],
                max_length=32,
                verbose_name="Тип заявки",
            ),
        ),
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
                    ("logistics_trip", "Внешний рейс"),
                ],
                default="manual",
                max_length=64,
                verbose_name="Тип источника",
            ),
        ),
        migrations.RunPython(seed_external_trip_service, migrations.RunPython.noop),
    ]
