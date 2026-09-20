from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0015_payment_promise_recon_discrepancy"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BillingStaffNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "audience",
                    models.CharField(
                        choices=[("manager", "Менеджер"), ("accountant", "Бухгалтер")],
                        default="manager",
                        max_length=32,
                        verbose_name="Аудитория",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("tariff_published", "Новый тариф"),
                            ("doc_returned", "Документ возвращён"),
                            ("doc_accepted", "Документ принят"),
                            ("doc_submitted", "Документ на проверке"),
                            ("payment", "Оплата"),
                            ("overdue", "Просрочка"),
                            ("no_price", "Нет цены"),
                            ("discrepancy", "Расхождение"),
                            ("upd", "УПД"),
                            ("promise", "Обещание"),
                            ("other", "Прочее"),
                        ],
                        db_index=True,
                        default="other",
                        max_length=32,
                        verbose_name="Тип",
                    ),
                ),
                ("title", models.CharField(max_length=255, verbose_name="Заголовок")),
                ("message", models.TextField(blank=True, verbose_name="Текст")),
                ("link_url", models.CharField(blank=True, max_length=512, verbose_name="Ссылка")),
                ("is_read", models.BooleanField(db_index=True, default=False, verbose_name="Прочитано")),
                ("read_at", models.DateTimeField(blank=True, null=True)),
                ("source_key", models.CharField(max_length=191, unique=True, verbose_name="Ключ идемпотентности")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "client",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="billing_staff_notifications",
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
                        related_name="created_billing_staff_notifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "recipient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="billing_staff_notifications",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Получатель",
                    ),
                ),
            ],
            options={
                "verbose_name": "Уведомление биллинга (сотрудник)",
                "verbose_name_plural": "Уведомления биллинга (сотрудники)",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="billingstaffnotification",
            index=models.Index(fields=["recipient", "is_read", "created_at"], name="billing_sta_recipie_a1b2c3_idx"),
        ),
        migrations.AddIndex(
            model_name="billingstaffnotification",
            index=models.Index(fields=["kind", "created_at"], name="billing_sta_kind_d4e5f6_idx"),
        ),
    ]
