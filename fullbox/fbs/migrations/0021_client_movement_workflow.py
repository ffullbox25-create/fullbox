from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fbs", "0020_replenishment_client_movement_links"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fbsclientmovementrequest",
            name="status",
            field=models.CharField(
                choices=[
                    ("submitted", "Ожидает менеджера"),
                    ("approved", "Передана на склад"),
                    ("warehouse_accepted", "Принята складом"),
                    ("in_progress", "В работе"),
                    ("moved", "Перемещена"),
                    ("awaiting_manager_confirmation", "Ожидает подтверждения менеджером"),
                    ("needs_clarification", "Требует уточнения"),
                    ("completed", "Выполнена"),
                    ("rejected", "Отклонена"),
                    ("canceled", "Отменена"),
                ],
                default="submitted",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="fbsclientmovementrequest",
            name="actual_moved_box_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="fbsclientmovementrequest",
            name="actual_moved_qty",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="fbsclientmovementrequest",
            name="billing_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fbsclientmovementrequest",
            name="clarification_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="fbsclientmovementrequest",
            name="manager_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fbsclientmovementrequest",
            name="manager_confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="manager_confirmed_fbs_client_movement_requests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="fbsclientmovementrequest",
            name="warehouse_accepted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fbsclientmovementrequest",
            name="warehouse_accepted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="accepted_fbs_client_movement_requests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="fbsclientmovementrequest",
            name="warehouse_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fbsclientmovementrequest",
            name="warehouse_confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="warehouse_confirmed_fbs_client_movement_requests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
