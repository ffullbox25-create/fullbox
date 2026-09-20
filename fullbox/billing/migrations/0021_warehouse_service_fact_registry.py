# Generated manually for warehouse service facts registry.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("billing", "0020_payment_external_id_idempotency"),
    ]

    operations = [
        migrations.AddField(
            model_name="warehouseservicefact",
            name="discrepancy_reason",
            field=models.TextField(blank=True, verbose_name="Причина расхождения"),
        ),
        migrations.AddField(
            model_name="warehouseservicefact",
            name="is_manual",
            field=models.BooleanField(default=True, verbose_name="Введено вручную"),
        ),
        migrations.AddField(
            model_name="warehouseservicefact",
            name="metadata",
            field=models.JSONField(blank=True, default=dict, verbose_name="Технические данные"),
        ),
        migrations.AddField(
            model_name="warehouseservicefact",
            name="performed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True, verbose_name="Когда выполнено"),
        ),
        migrations.AddField(
            model_name="warehouseservicefact",
            name="planned_quantity",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                max_digits=14,
                null=True,
                verbose_name="Плановое количество",
            ),
        ),
        migrations.AddField(
            model_name="warehouseservicefact",
            name="source",
            field=models.CharField(blank=True, default="warehouse_manual", max_length=64, verbose_name="Источник"),
        ),
        migrations.AddField(
            model_name="warehouseservicefact",
            name="status",
            field=models.CharField(
                choices=[
                    ("sent_to_billing", "Передано в биллинг"),
                    ("waiting_manager_review", "На проверке менеджером"),
                    ("approved", "Одобрено"),
                    ("needs_clarification", "Требует уточнения"),
                    ("charged", "Начислено"),
                    ("cancelled", "Отменено"),
                ],
                db_index=True,
                default="sent_to_billing",
                max_length=32,
                verbose_name="Статус проверки",
            ),
        ),
        migrations.AddField(
            model_name="warehouseservicefact",
            name="warehouse_label",
            field=models.CharField(blank=True, max_length=255, verbose_name="Склад / участок"),
        ),
        migrations.AddIndex(
            model_name="warehouseservicefact",
            index=models.Index(fields=["status", "reported_at"], name="billing_war_status_809188_idx"),
        ),
        migrations.CreateModel(
            name="WarehouseServiceFactAttachment",
            fields=[
                (
                    "id",
                    models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                (
                    "file",
                    models.FileField(upload_to="billing/warehouse-service-facts/%Y/%m/", verbose_name="Файл"),
                ),
                ("comment", models.CharField(blank=True, max_length=255, verbose_name="Комментарий")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Когда загружено")),
                (
                    "fact",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attachments",
                        to="billing.warehouseservicefact",
                        verbose_name="Факт услуги",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="uploaded_warehouse_service_fact_attachments",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто загрузил",
                    ),
                ),
            ],
            options={
                "verbose_name": "Вложение к факту услуги склада",
                "verbose_name_plural": "Вложения к фактам услуг склада",
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
