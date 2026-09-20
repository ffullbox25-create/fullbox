from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0036_workstation_cart_capacity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="fbsworkstation",
            name="shift_controller",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fbs_controller_workstations",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Контролер на смене",
            ),
        ),
        migrations.AddField(
            model_name="fbsworkstation",
            name="shift_heartbeat_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Последняя активность контролера",
            ),
        ),
        migrations.AddField(
            model_name="fbsworkstation",
            name="shift_status",
            field=models.CharField(
                choices=[
                    ("closed", "Смена закрыта"),
                    ("available", "Принимает тележки"),
                    ("paused", "Пауза"),
                ],
                default="closed",
                max_length=16,
                verbose_name="Статус смены контролера",
            ),
        ),
        migrations.AddIndex(
            model_name="fbsworkstation",
            index=models.Index(
                fields=["shift_status", "shift_heartbeat_at"],
                name="fbs_ws_shift_heartbeat_idx",
            ),
        ),
    ]
