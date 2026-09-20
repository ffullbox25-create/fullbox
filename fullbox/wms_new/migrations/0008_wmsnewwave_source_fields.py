from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("wms_new", "0007_wmsnewbox_wmsnewboxitem_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="wmsnewwave",
            name="assigned_to",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_wms_new_waves",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="wmsnewwave",
            name="cart_name",
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name="wmsnewwave",
            name="is_manual",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="wmsnewwave",
            name="last_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="wmsnewwave",
            name="pilot_revision",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="wmsnewwave",
            name="source_batch_id",
            field=models.BigIntegerField(blank=True, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="wmsnewwave",
            name="source_created_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="wmsnewwave",
            name="source_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="wmsnewwave",
            name="source_status",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="wmsnewwave",
            name="source_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="wmsnewwave",
            name="workstation_name",
            field=models.CharField(blank=True, max_length=128),
        ),
    ]
