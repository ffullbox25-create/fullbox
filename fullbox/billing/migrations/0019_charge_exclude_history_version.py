# Generated manually for charge exclude / history / optimistic lock

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0018_warehouse_service_fact"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="applicationcharge",
            name="edit_version",
            field=models.PositiveIntegerField(default=1, verbose_name="Версия для оптимистичной блокировки"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="exclude_comment",
            field=models.TextField(blank=True, verbose_name="Комментарий к исключению"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="exclude_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("not_performed", "Услуга фактически не оказывалась"),
                    ("wrong_auto", "Автоматика определила неправильную операцию"),
                    ("duplicate", "Дублирующее начисление"),
                    ("included_in_other", "Услуга включена в другую услугу"),
                    ("other", "Другое"),
                ],
                max_length=32,
                verbose_name="Причина исключения",
            ),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="excluded_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Исключено когда"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="excluded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="excluded_billing_charges",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Кто исключил",
            ),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="is_excluded",
            field=models.BooleanField(db_index=True, default=False, verbose_name="Исключено из расчёта"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="original_quantity",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                max_digits=14,
                null=True,
                verbose_name="Первоначальное количество",
            ),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="qty_change_basis",
            field=models.CharField(
                blank=True,
                choices=[
                    ("warehouse", "Данные склада"),
                    ("fact", "Фактическое количество"),
                    ("boxes", "Количество коробов"),
                    ("pallets", "Количество палет"),
                    ("units", "Количество единиц товара"),
                    ("manual", "Ручная корректировка"),
                    ("other", "Другое"),
                ],
                max_length=32,
                verbose_name="Основание изменения количества",
            ),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="qty_change_comment",
            field=models.TextField(blank=True, verbose_name="Комментарий к изменению количества"),
        ),
        migrations.AddIndex(
            model_name="applicationcharge",
            index=models.Index(fields=["application", "is_excluded"], name="billing_app_applica_63e77d_idx"),
        ),
        migrations.CreateModel(
            name="ApplicationChargeHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "change_type",
                    models.CharField(
                        choices=[
                            ("service", "Вид услуги"),
                            ("qty", "Количество"),
                            ("tariff", "Тариф"),
                            ("exclude", "Исключение"),
                            ("restore", "Восстановление"),
                            ("add", "Добавление"),
                            ("confirm", "Подтверждение"),
                            ("unconfirm", "Снятие подтверждения"),
                            ("recalc", "Пересчёт"),
                        ],
                        db_index=True,
                        max_length=32,
                        verbose_name="Тип изменения",
                    ),
                ),
                ("old_service_name", models.CharField(blank=True, max_length=255, verbose_name="Старая услуга")),
                ("new_service_name", models.CharField(blank=True, max_length=255, verbose_name="Новая услуга")),
                (
                    "old_quantity",
                    models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True, verbose_name="Старое количество"),
                ),
                (
                    "new_quantity",
                    models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True, verbose_name="Новое количество"),
                ),
                (
                    "old_tariff",
                    models.DecimalField(blank=True, decimal_places=4, max_digits=14, null=True, verbose_name="Старый тариф"),
                ),
                (
                    "new_tariff",
                    models.DecimalField(blank=True, decimal_places=4, max_digits=14, null=True, verbose_name="Новый тариф"),
                ),
                (
                    "old_total",
                    models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="Старая сумма"),
                ),
                (
                    "new_total",
                    models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="Новая сумма"),
                ),
                ("reason", models.CharField(blank=True, max_length=64, verbose_name="Причина")),
                ("comment", models.TextField(blank=True, verbose_name="Комментарий")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "application",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="charge_history",
                        to="billing.billingapplication",
                        verbose_name="Заявка",
                    ),
                ),
                (
                    "charge",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="history_entries",
                        to="billing.applicationcharge",
                        verbose_name="Начисление",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="billing_charge_history",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Пользователь",
                    ),
                ),
            ],
            options={
                "verbose_name": "История начисления",
                "verbose_name_plural": "История начислений",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="applicationchargehistory",
            index=models.Index(fields=["charge", "-created_at"], name="billing_app_charge__01f825_idx"),
        ),
        migrations.AddIndex(
            model_name="applicationchargehistory",
            index=models.Index(fields=["application", "-created_at"], name="billing_app_applica_65ba51_idx"),
        ),
    ]
