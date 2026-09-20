from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("fbs", "0057_fbs_client_stock_policy"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FbsMarketplaceQueuePriority",
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
                    "marketplace",
                    models.CharField(
                        choices=[("wb", "Wildberries"), ("ozon", "Ozon")],
                        max_length=16,
                        unique=True,
                        verbose_name="Маркетплейс",
                    ),
                ),
                (
                    "priority",
                    models.PositiveSmallIntegerField(
                        default=100,
                        help_text="Меньшее число означает более высокий приоритет.",
                        verbose_name="Приоритет",
                    ),
                ),
                ("effective_from", models.DateField(verbose_name="Действует с")),
                ("is_active", models.BooleanField(default=True, verbose_name="Включен")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="updated_fbs_marketplace_queue_priorities",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кем изменено",
                    ),
                ),
            ],
            options={
                "verbose_name": "Приоритет маркетплейса в очереди FBS",
                "verbose_name_plural": "Приоритеты маркетплейсов в очереди FBS",
                "db_table": "fbs_marketplace_queue_priority",
                "ordering": ["priority", "marketplace"],
            },
        ),
        migrations.AddField(
            model_name="fbspickbatch",
            name="manual_queue_date",
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name="Дата ручного порядка очереди",
            ),
        ),
        migrations.AddField(
            model_name="fbspickbatch",
            name="manual_queue_rank",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                verbose_name="Ручное место в очереди",
            ),
        ),
        migrations.AddField(
            model_name="fbspickbatch",
            name="queue_order_updated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reordered_fbs_pick_batches",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Кем изменен порядок очереди",
            ),
        ),
        migrations.AddConstraint(
            model_name="fbsmarketplacequeuepriority",
            constraint=models.CheckConstraint(
                condition=models.Q(("priority__gte", 1), ("priority__lte", 100)),
                name="fbs_marketplace_queue_priority_1_100",
            ),
        ),
        migrations.AddIndex(
            model_name="fbspickbatch",
            index=models.Index(
                fields=["manual_queue_date", "manual_queue_rank"],
                name="fbs_pick_ba_manual__e48602_idx",
            ),
        ),
    ]
