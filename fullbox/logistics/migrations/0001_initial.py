from django.conf import settings
from django.db import migrations, models

import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("employees", "0008_employee_logistician_role"),
        ("shipping", "0006_shippingorderattachment"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="LogisticsTrip",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("number", models.CharField(db_index=True, max_length=32, unique=True, verbose_name="Номер рейса")),
                ("trip_date", models.DateField(blank=True, null=True, verbose_name="Дата рейса")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Черновик"),
                            ("planned", "Спланирован"),
                            ("loading", "Погрузка"),
                            ("departed", "В рейсе"),
                            ("completed", "Завершен"),
                            ("canceled", "Отменен"),
                        ],
                        default="draft",
                        max_length=32,
                        verbose_name="Статус",
                    ),
                ),
                (
                    "vehicle_type",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("fulfillment", "Транспорт Fullbox"),
                            ("client", "Транспорт клиента"),
                            ("hired", "Наемный транспорт"),
                            ("other", "Другое"),
                        ],
                        default="",
                        max_length=32,
                        verbose_name="Тип транспорта",
                    ),
                ),
                ("vehicle_name", models.CharField(blank=True, max_length=128, verbose_name="Транспорт")),
                ("vehicle_number", models.CharField(blank=True, max_length=32, verbose_name="Номер авто")),
                ("driver_name", models.CharField(blank=True, max_length=255, verbose_name="Водитель")),
                ("driver_phone", models.CharField(blank=True, max_length=32, verbose_name="Телефон водителя")),
                ("route_comment", models.TextField(blank=True, verbose_name="Комментарий по маршруту")),
                ("loading_comment", models.TextField(blank=True, verbose_name="Комментарий по погрузке")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создан")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлен")),
                (
                    "assigned_logistician",
                    models.ForeignKey(
                        blank=True,
                        limit_choices_to={"role": "logistician"},
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="logistics_trips",
                        to="employees.employee",
                        verbose_name="Логист",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_logistics_trips",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Создал",
                    ),
                ),
            ],
            options={
                "verbose_name": "Рейс логистики",
                "verbose_name_plural": "Рейсы логистики",
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["status", "trip_date"], name="logistics_l_status_a725ef_idx")],
            },
        ),
        migrations.CreateModel(
            name="LogisticsTripOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("loading_sequence", models.PositiveIntegerField(default=0, verbose_name="Очередь погрузки")),
                ("delivery_sequence", models.PositiveIntegerField(default=0, verbose_name="Очередь доставки")),
                ("comment", models.CharField(blank=True, max_length=255, verbose_name="Комментарий")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создан")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлен")),
                (
                    "shipping_order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="logistics_links",
                        to="shipping.shippingorder",
                        verbose_name="Заявка на отгрузку",
                    ),
                ),
                (
                    "trip",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="orders",
                        to="logistics.logisticstrip",
                        verbose_name="Рейс",
                    ),
                ),
            ],
            options={
                "verbose_name": "Заявка в рейсе",
                "verbose_name_plural": "Заявки в рейсе",
                "ordering": ["loading_sequence", "delivery_sequence", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="logisticstriporder",
            constraint=models.UniqueConstraint(fields=("trip", "shipping_order"), name="uniq_logistics_trip_shipping_order"),
        ),
    ]
