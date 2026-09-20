import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0013_extra_service_request"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="billingact",
            name="accountant_comment",
            field=models.TextField(blank=True, verbose_name="Комментарий бухгалтера"),
        ),
        migrations.AddField(
            model_name="billingact",
            name="review_status",
            field=models.CharField(
                choices=[
                    ("local", "Черновик менеджера"),
                    ("submitted", "На проверке у бухгалтера"),
                    ("returned", "Возвращён менеджеру"),
                    ("accepted", "Принят бухгалтером"),
                ],
                db_index=True,
                default="local",
                max_length=32,
                verbose_name="Проверка бухгалтером",
            ),
        ),
        migrations.AddField(
            model_name="billingact",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Проверен бухгалтером"),
        ),
        migrations.AddField(
            model_name="billingact",
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reviewed_billing_acts",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="billingact",
            name="submitted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Передан бухгалтеру"),
        ),
        migrations.AddField(
            model_name="billingact",
            name="submitted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="submitted_billing_acts",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="clientinvoice",
            name="accountant_comment",
            field=models.TextField(blank=True, verbose_name="Комментарий бухгалтера"),
        ),
        migrations.AddField(
            model_name="clientinvoice",
            name="review_status",
            field=models.CharField(
                choices=[
                    ("local", "Черновик менеджера"),
                    ("submitted", "На проверке у бухгалтера"),
                    ("returned", "Возвращён менеджеру"),
                    ("accepted", "Принят бухгалтером"),
                ],
                db_index=True,
                default="local",
                max_length=32,
                verbose_name="Проверка бухгалтером",
            ),
        ),
        migrations.AddField(
            model_name="clientinvoice",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Проверен бухгалтером"),
        ),
        migrations.AddField(
            model_name="clientinvoice",
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reviewed_client_invoices",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="clientinvoice",
            name="submitted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Передан бухгалтеру"),
        ),
        migrations.AddField(
            model_name="clientinvoice",
            name="submitted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="submitted_client_invoices",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="billingact",
            index=models.Index(fields=["review_status", "submitted_at"], name="billing_bil_review__1460d0_idx"),
        ),
        migrations.AddIndex(
            model_name="clientinvoice",
            index=models.Index(fields=["review_status", "submitted_at"], name="billing_cli_review__c4df52_idx"),
        ),
        migrations.CreateModel(
            name="UpdRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_from", models.DateField(blank=True, null=True, verbose_name="Период с")),
                ("period_to", models.DateField(blank=True, null=True, verbose_name="Период по")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("requested", "Запрошен"),
                            ("in_progress", "В работе"),
                            ("issued", "Выпущен"),
                            ("rejected", "Отклонён"),
                            ("cancelled", "Отменён"),
                        ],
                        db_index=True,
                        default="requested",
                        max_length=32,
                        verbose_name="Статус",
                    ),
                ),
                ("comment", models.TextField(blank=True, verbose_name="Комментарий менеджера")),
                ("accountant_comment", models.TextField(blank=True, verbose_name="Комментарий бухгалтера")),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "act",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="upd_requests",
                        to="billing.billingact",
                    ),
                ),
                (
                    "application",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="upd_requests",
                        to="billing.billingapplication",
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="upd_requests",
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
                        related_name="created_upd_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="upd_requests",
                        to="billing.clientinvoice",
                    ),
                ),
                (
                    "processed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="processed_upd_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Запрос УПД",
                "verbose_name_plural": "Запросы УПД",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="updrequest",
            index=models.Index(fields=["client", "status"], name="billing_upd_client__562727_idx"),
        ),
        migrations.AddIndex(
            model_name="updrequest",
            index=models.Index(fields=["status", "created_at"], name="billing_upd_status_eabd3b_idx"),
        ),
    ]
