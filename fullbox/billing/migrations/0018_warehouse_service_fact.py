# Generated manually for WarehouseServiceFact (storekeeper service qty, no prices)

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0017_charge_service_changed"),
        ("sku", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WarehouseServiceFact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "order_type",
                    models.CharField(
                        choices=[
                            ("receiving", "Приёмка"),
                            ("processing", "Обработка"),
                            ("packing", "Упаковка"),
                            ("shipping", "Отгрузка"),
                        ],
                        db_index=True,
                        max_length=32,
                        verbose_name="Тип заявки",
                    ),
                ),
                ("order_id", models.CharField(db_index=True, max_length=128, verbose_name="Номер заявки")),
                ("service_name_snapshot", models.CharField(blank=True, max_length=255, verbose_name="Название услуги")),
                ("quantity", models.DecimalField(decimal_places=3, max_digits=14, verbose_name="Количество")),
                ("unit", models.CharField(blank=True, max_length=32, verbose_name="Единица")),
                ("comment", models.CharField(blank=True, max_length=255, verbose_name="Комментарий")),
                ("reported_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Когда указано")),
                ("source_key", models.CharField(blank=True, db_index=True, max_length=160, verbose_name="Ключ источника")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "application",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="warehouse_service_facts",
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
                        related_name="warehouse_service_facts",
                        to="billing.applicationcharge",
                        verbose_name="Начисление",
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="warehouse_service_facts",
                        to="sku.agency",
                        verbose_name="Клиент",
                    ),
                ),
                (
                    "reported_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="reported_warehouse_service_facts",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто указал",
                    ),
                ),
                (
                    "service",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="warehouse_service_facts",
                        to="billing.billingservice",
                        verbose_name="Услуга",
                    ),
                ),
            ],
            options={
                "verbose_name": "Факт услуги склада",
                "verbose_name_plural": "Факты услуг склада",
                "ordering": ["order_type", "order_id", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="warehouseservicefact",
            index=models.Index(fields=["client", "order_type", "order_id"], name="billing_war_client__5e38ce_idx"),
        ),
        migrations.AddIndex(
            model_name="warehouseservicefact",
            index=models.Index(fields=["order_type", "order_id"], name="billing_war_order_t_d1b6d5_idx"),
        ),
        migrations.AddConstraint(
            model_name="warehouseservicefact",
            constraint=models.UniqueConstraint(
                fields=("client", "order_type", "order_id", "service"),
                name="uniq_warehouse_service_fact_order_service",
            ),
        ),
    ]
