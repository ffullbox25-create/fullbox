import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("logistics", "0009_driver_preliminary_assignment"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
    ]

    operations = [
        migrations.AddField(
            model_name="logisticstrip",
            name="trip_kind",
            field=models.CharField(
                choices=[
                    ("internal", "По внутренним заявкам"),
                    ("external", "Внешний рейс"),
                ],
                db_index=True,
                default="internal",
                max_length=16,
                verbose_name="Тип рейса",
            ),
        ),
        migrations.CreateModel(
            name="LogisticsExternalTrip",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("pickup_address", models.TextField(verbose_name="Точка А — откуда забрать")),
                ("delivery_address", models.TextField(verbose_name="Точка Б — куда доставить")),
                ("cargo_description", models.TextField(blank=True, verbose_name="Описание груза")),
                ("pickup_contact", models.CharField(blank=True, max_length=255, verbose_name="Контакт в точке А")),
                ("pickup_phone", models.CharField(blank=True, max_length=32, verbose_name="Телефон в точке А")),
                ("delivery_contact", models.CharField(blank=True, max_length=255, verbose_name="Контакт в точке Б")),
                ("delivery_phone", models.CharField(blank=True, max_length=32, verbose_name="Телефон в точке Б")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлено")),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="external_logistics_trips",
                        to="sku.agency",
                        verbose_name="Клиент для биллинга",
                    ),
                ),
                (
                    "trip",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="external_details",
                        to="logistics.logisticstrip",
                        verbose_name="Рейс",
                    ),
                ),
            ],
            options={
                "verbose_name": "Детали внешнего рейса",
                "verbose_name_plural": "Детали внешних рейсов",
            },
        ),
        migrations.AddConstraint(
            model_name="logisticsexternaltrip",
            constraint=models.CheckConstraint(
                condition=~models.Q(pickup_address="") & ~models.Q(delivery_address=""),
                name="log_ext_route_nonempty",
            ),
        ),
    ]
