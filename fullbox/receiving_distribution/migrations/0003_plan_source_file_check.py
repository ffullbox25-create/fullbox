from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import receiving_distribution.models


class Migration(migrations.Migration):

    dependencies = [
        ("receiving_distribution", "0002_allocation_qty_accepted"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="receivingdistributionplan",
            name="check_payload",
            field=models.JSONField(blank=True, default=dict, verbose_name="Результат проверки"),
        ),
        migrations.AddField(
            model_name="receivingdistributionplan",
            name="check_status",
            field=models.CharField(
                blank=True,
                default="",
                help_text="empty|uploaded|checking|ok|errors|warnings|stale",
                max_length=32,
                verbose_name="Статус проверки файла",
            ),
        ),
        migrations.AddField(
            model_name="receivingdistributionplan",
            name="checked_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Проверено"),
        ),
        migrations.AddField(
            model_name="receivingdistributionplan",
            name="source_file",
            field=models.FileField(
                blank=True,
                max_length=512,
                null=True,
                upload_to=receiving_distribution.models.distribution_source_upload_to,
                verbose_name="Исходный Excel",
            ),
        ),
        migrations.AddField(
            model_name="receivingdistributionplan",
            name="source_filename",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="Имя файла"),
        ),
        migrations.AddField(
            model_name="receivingdistributionplan",
            name="updated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="updated_receiving_distribution_plans",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Изменил",
            ),
        ),
    ]
