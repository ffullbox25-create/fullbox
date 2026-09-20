from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fbs", "0021_client_movement_workflow"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fbsreplenishmentplan",
            name="status",
            field=models.CharField(
                choices=[
                    ("proposed", "Предложен программой"),
                    ("confirmed", "Подтвержден кладовщиком"),
                    ("in_progress", "В работе"),
                    ("awaiting_pack", "Ожидает укладки в короб"),
                    ("done", "Выполнен"),
                    ("blocked", "Заблокирован"),
                    ("canceled", "Отменен"),
                ],
                default="proposed",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="fbsreplenishmentplan",
            name="staging_location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="fbs_staging_replenishment_plans",
                to="sklad.warehouselocation",
            ),
        ),
        migrations.AlterField(
            model_name="fbsreplenishmentline",
            name="target_box",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replenishment_lines",
                to="fbs.fbsbox",
            ),
        ),
        migrations.AlterField(
            model_name="fbsreplenishmentallocation",
            name="status",
            field=models.CharField(
                choices=[
                    ("reserved", "Зарезервировано"),
                    ("in_progress", "В работе"),
                    ("staged", "Передано кладовщику"),
                    ("done", "Перемещено"),
                    ("canceled", "Отменено"),
                ],
                default="reserved",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="fbsreplenishmentallocation",
            name="target_box",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replenishment_allocations",
                to="fbs.fbsbox",
            ),
        ),
        migrations.AddField(
            model_name="fbsreplenishmentallocation",
            name="qty_staged",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="fbsreplenishmentallocation",
            name="staged_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fbsreplenishmentallocation",
            name="staged_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="staged_fbs_replenishment_allocations",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="fbsreplenishmentallocation",
            name="uniq_fbs_replenishment_allocation",
        ),
        migrations.AddConstraint(
            model_name="fbsreplenishmentallocation",
            constraint=models.UniqueConstraint(
                fields=("line", "source_snapshot"),
                name="uniq_fbs_replenishment_allocation",
            ),
        ),
        migrations.AddConstraint(
            model_name="fbsreplenishmentallocation",
            constraint=models.CheckConstraint(
                condition=models.Q(("qty_planned__gte", models.F("qty_staged"))),
                name="fbs_allocation_staged_lte_planned",
            ),
        ),
    ]
