from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0051_fbsozoncredential"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FbsControllerPolicy",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "skip_repeat_wb_label_scan",
                    models.BooleanField(
                        default=False,
                        verbose_name="Не повторять скан этикетки заказа WB",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "controller",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="fbs_controller_policy",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="FBS-контролёр",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="updated_fbs_controller_policies",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кем изменено",
                    ),
                ),
            ],
            options={
                "verbose_name": "Настройка FBS-контролёра",
                "verbose_name_plural": "Настройки FBS-контролёров",
                "db_table": "fbs_controller_policy",
                "ordering": ["controller_id"],
            },
        ),
        migrations.AddField(
            model_name="fbscontrollertoteorder",
            name="primary_order_label_scan_reused",
            field=models.BooleanField(
                default=False,
                verbose_name=(
                    "Первичный скан этикетки использован для проверки состава"
                ),
            ),
        ),
    ]
