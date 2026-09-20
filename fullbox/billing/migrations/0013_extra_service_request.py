import billing.models
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0012_confirm_existing_charges"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ExtraServiceRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("service_date", models.DateField(db_index=True, verbose_name="Дата оказания")),
                ("quantity", models.DecimalField(decimal_places=3, max_digits=14, verbose_name="Количество")),
                ("unit", models.CharField(blank=True, max_length=32, verbose_name="Единица")),
                ("warehouse_label", models.CharField(blank=True, max_length=128, verbose_name="Склад")),
                ("description", models.TextField(blank=True, verbose_name="Описание")),
                ("reason", models.TextField(blank=True, verbose_name="Причина запроса")),
                ("performer", models.CharField(blank=True, max_length=255, verbose_name="Исполнитель")),
                ("internal_comment", models.TextField(blank=True, verbose_name="Внутренний комментарий")),
                ("client_comment", models.TextField(blank=True, verbose_name="Комментарий для клиента")),
                (
                    "attachment",
                    models.FileField(
                        blank=True,
                        null=True,
                        upload_to=billing.models.billing_document_upload_to,
                        verbose_name="Подтверждение",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("needs_price", "Требуется настройка цены"),
                            ("charged", "Начисление создано"),
                            ("cancelled", "Отменён"),
                        ],
                        db_index=True,
                        default="needs_price",
                        max_length=32,
                        verbose_name="Статус",
                    ),
                ),
                ("price_note", models.TextField(blank=True, verbose_name="Примечание по цене")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "application",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="extra_service_requests",
                        to="billing.billingapplication",
                        verbose_name="Заявка биллинга",
                    ),
                ),
                (
                    "charge",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="extra_service_requests",
                        to="billing.applicationcharge",
                        verbose_name="Начисление",
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="extra_service_requests",
                        to="sku.agency",
                        verbose_name="Клиент",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_extra_service_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "service",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="extra_service_requests",
                        to="billing.billingservice",
                        verbose_name="Услуга",
                    ),
                ),
            ],
            options={
                "verbose_name": "Запрос доп. услуги",
                "verbose_name_plural": "Запросы доп. услуг",
                "ordering": ["-service_date", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="extraservicerequest",
            index=models.Index(fields=["client", "status"], name="billing_ext_client__057a34_idx"),
        ),
        migrations.AddIndex(
            model_name="extraservicerequest",
            index=models.Index(fields=["status", "service_date"], name="billing_ext_status_54208e_idx"),
        ),
    ]
