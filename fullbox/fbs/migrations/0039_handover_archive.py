from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fbs", "0038_pick_batch_cart_release"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fbshandoverbatch",
            name="status",
            field=models.CharField(
                choices=[
                    ("open", "Сканирование"),
                    ("ready", "Готова к отгрузке"),
                    ("dispatched", "Передана водителю"),
                    ("accepted", "Принята маркетплейсом"),
                    ("problem", "Проблема"),
                    ("archived", "Архив"),
                ],
                default="open",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="fbshandoverbatch",
            name="archive_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="fbshandoverbatch",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fbshandoverbatch",
            name="archived_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="archived_fbs_handover_batches",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
