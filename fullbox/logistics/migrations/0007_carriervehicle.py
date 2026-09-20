from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("head_manager", "0003_carrier_bank_address_carrier_bank_bik_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("logistics", "0006_shippingroutingstate_delivery_failed"),
    ]

    operations = [
        migrations.CreateModel(
            name="CarrierVehicle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("vehicle_name", models.CharField(blank=True, max_length=128, verbose_name="Марка / модель")),
                ("vehicle_number", models.CharField(max_length=32, verbose_name="Госномер")),
                ("driver_name", models.CharField(max_length=255, verbose_name="ФИО водителя")),
                ("driver_phone", models.CharField(max_length=32, verbose_name="Телефон водителя")),
                (
                    "max_weight_kg",
                    models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True, verbose_name="Грузоподъёмность, кг"),
                ),
                (
                    "max_volume_m3",
                    models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True, verbose_name="Вместимость, м³"),
                ),
                ("max_pallets", models.PositiveIntegerField(blank=True, null=True, verbose_name="Максимум паллет")),
                ("is_default", models.BooleanField(default=False, verbose_name="По умолчанию")),
                ("is_active", models.BooleanField(db_index=True, default=True, verbose_name="Активен")),
                ("comment", models.CharField(blank=True, max_length=255, verbose_name="Комментарий")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлено")),
                (
                    "carrier",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="vehicles",
                        to="head_manager.carrier",
                        verbose_name="Перевозчик",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_carrier_vehicles",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Создал",
                    ),
                ),
            ],
            options={
                "verbose_name": "Автомобиль перевозчика",
                "verbose_name_plural": "Автомобили перевозчиков",
                "ordering": ("carrier__name", "-is_default", "vehicle_number", "id"),
                "indexes": [models.Index(fields=["carrier", "is_active"], name="log_carrier_active_idx")],
                "constraints": [
                    models.UniqueConstraint(fields=("carrier", "vehicle_number"), name="uniq_carrier_vehicle_number"),
                    models.UniqueConstraint(
                        condition=models.Q(("is_active", True), ("is_default", True)),
                        fields=("carrier",),
                        name="uniq_active_default_vehicle_per_carrier",
                    ),
                ],
            },
        ),
    ]
