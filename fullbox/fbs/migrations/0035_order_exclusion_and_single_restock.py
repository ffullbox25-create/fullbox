from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fbs", "0034_handover_order_composition_verification"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fbspickrestockrequest",
            name="batch",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="pick_restock_request",
                to="fbs.fbspickbatch",
            ),
        ),
        migrations.AlterField(
            model_name="fbspickrestockrequest",
            name="status",
            field=models.CharField(
                choices=[
                    ("waiting_marketplace", "Проверяется маркетплейсом"),
                    ("queued", "Ожидает подборщика"),
                    ("in_progress", "Возвращается"),
                    ("completed", "Возвращено"),
                    ("failed", "Ошибка исключения"),
                    ("canceled", "Отменено"),
                ],
                default="queued",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="fbspickrestockrequest",
            name="order",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="pick_restock_requests",
                to="fbs.fbsorder",
            ),
        ),
        migrations.AddField(
            model_name="fbspickrestockrequest",
            name="handover_assignment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="pick_restock_requests",
                to="fbs.fbshandoverorderassignment",
            ),
        ),
        migrations.AddField(
            model_name="fbspickrestockrequest",
            name="reason_code",
            field=models.CharField(
                blank=True,
                choices=[
                    ("client_canceled", "Отменен клиентом или покупателем"),
                    ("damaged", "Товар поврежден"),
                    ("wrong_product", "Несоответствие товара"),
                    ("metadata_error", "Ошибка КИЗ или срока годности"),
                    ("marketplace_error", "Ошибка маркетплейса"),
                    ("other", "Другая причина"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="fbspickrestockrequest",
            name="marketplace_action",
            field=models.CharField(
                choices=[
                    ("none", "Не требуется"),
                    ("verify_cancel", "Проверить отмену маркетплейса"),
                    ("seller_cancel", "Отменить продавцом"),
                ],
                default="none",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="fbspickrestockrequest",
            name="marketplace_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="fbspickrestockrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("order__isnull", False),
                    (
                        "status__in",
                        [
                            "waiting_marketplace",
                            "queued",
                            "in_progress",
                            "failed",
                        ],
                    ),
                ),
                fields=("order",),
                name="uniq_active_fbs_order_restock",
            ),
        ),
        migrations.AddField(
            model_name="fbshandoverorder",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "В отгрузке"),
                    ("return_pending", "Ожидает возврата"),
                    ("excluded", "Исключен"),
                ],
                default="active",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="fbshandoverorder",
            name="exclusion_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="fbshandoverorder",
            name="excluded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fbshandoverorder",
            name="excluded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="excluded_fbs_handover_orders",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="fbshandoverorder",
            index=models.Index(
                fields=["box", "status"],
                name="fbs_handov_box_status_idx",
            ),
        ),
    ]
