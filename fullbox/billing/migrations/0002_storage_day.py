# Generated manually for storage daily billing

from django.db import migrations, models
import django.db.models.deletion


def seed_storage_service(apps, schema_editor):
    BillingService = apps.get_model("billing", "BillingService")
    BillingService.objects.get_or_create(
        code="storage_pallet_day",
        defaults={
            "name": "Хранение палеты/день",
            "unit": "пал.",
            "vat_rate": "20",
            "is_active": True,
        },
    )


def unseed_storage_service(apps, schema_editor):
    BillingService = apps.get_model("billing", "BillingService")
    BillingService.objects.filter(code="storage_pallet_day").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
    ]

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
                ],
                default="manual",
                max_length=64,
                verbose_name="Тип источника",
            ),
        ),
        migrations.CreateModel(
            name="BillingStorageDay",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("day", models.DateField(db_index=True, verbose_name="Дата")),
                ("pallet_count", models.PositiveIntegerField(default=0, verbose_name="Палет")),
                ("zone_counts", models.JSONField(blank=True, default=dict, verbose_name="По зонам")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "application",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="storage_days",
                        to="billing.billingapplication",
                        verbose_name="Заявка хранения",
                    ),
                ),
                (
                    "charge",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="storage_days",
                        to="billing.applicationcharge",
                        verbose_name="Начисление",
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="billing_storage_days",
                        to="sku.agency",
                        verbose_name="Клиент",
                    ),
                ),
            ],
            options={
                "verbose_name": "День хранения",
                "verbose_name_plural": "Дни хранения",
                "ordering": ["-day", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="billingstorageday",
            index=models.Index(fields=["day"], name="billing_bil_day_7c5a21_idx"),
        ),
        migrations.AddIndex(
            model_name="billingstorageday",
            index=models.Index(fields=["client", "day"], name="billing_bil_client_9a1f02_idx"),
        ),
        migrations.AddIndex(
            model_name="billingstorageday",
            index=models.Index(fields=["application", "day"], name="billing_bil_applica_4e8b33_idx"),
        ),
        migrations.AddConstraint(
            model_name="billingstorageday",
            constraint=models.UniqueConstraint(fields=("client", "day"), name="uniq_billing_storage_day_client"),
        ),
        migrations.RunPython(seed_storage_service, unseed_storage_service),
    ]
