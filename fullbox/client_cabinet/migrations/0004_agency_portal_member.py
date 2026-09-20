# Generated manually for AgencyPortalMember

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("client_cabinet", "0003_notifications_chat"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AgencyPortalMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("last_name", models.CharField(max_length=120, verbose_name="Фамилия")),
                ("first_name", models.CharField(max_length=120, verbose_name="Имя")),
                ("email", models.EmailField(max_length=254, verbose_name="Email")),
                ("position", models.CharField(blank=True, default="", max_length=255, verbose_name="Должность")),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("admin", "Администратор"),
                            ("manager", "Менеджер"),
                            ("accountant", "Бухгалтер"),
                            ("operator", "Оператор"),
                            ("custom", "Свой доступ"),
                        ],
                        default="custom",
                        max_length=32,
                        verbose_name="Роль",
                    ),
                ),
                ("sections", models.JSONField(blank=True, default=list, verbose_name="Разделы")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активен")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "agency",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="portal_members",
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
                        related_name="created_agency_portal_members",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто добавил",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="agency_portal_memberships",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Пользователь",
                    ),
                ),
            ],
            options={
                "verbose_name": "Сотрудник ЛК клиента",
                "verbose_name_plural": "Сотрудники ЛК клиента",
                "ordering": ["last_name", "first_name", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="agencyportalmember",
            index=models.Index(fields=["agency", "is_active"], name="client_cabi_agency__7f1a2b_idx"),
        ),
        migrations.AddIndex(
            model_name="agencyportalmember",
            index=models.Index(fields=["user", "is_active"], name="client_cabi_user_id_3c4d5e_idx"),
        ),
        migrations.AddConstraint(
            model_name="agencyportalmember",
            constraint=models.UniqueConstraint(fields=("agency", "email"), name="uniq_agency_portal_member_email"),
        ),
    ]
