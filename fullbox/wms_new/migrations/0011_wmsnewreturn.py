import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("wms_new", "0010_wmsnewshipment_wmsnewshipmentbox_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WmsNewReturn",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_request_id", models.BigIntegerField(blank=True, null=True, unique=True)),
                ("source_batch_id", models.BigIntegerField(blank=True, null=True)),
                ("status", models.CharField(choices=[("waiting_marketplace", "Проверяется маркетплейсом"), ("queued", "Ожидает подборщика"), ("in_progress", "Возвращается"), ("completed", "Возвращено"), ("failed", "Ошибка возврата"), ("cancelled", "Отменено")], default="queued", max_length=24)),
                ("reason_code", models.CharField(blank=True, max_length=32)),
                ("reason", models.TextField(blank=True)),
                ("planned_qty", models.PositiveIntegerField(default=1)),
                ("returned_qty", models.PositiveIntegerField(default=0)),
                ("shipped_at", models.DateTimeField(blank=True, null=True)),
                ("returned_at", models.DateTimeField(blank=True, null=True)),
                ("source_created_at", models.DateTimeField(blank=True, null=True)),
                ("source_updated_at", models.DateTimeField(blank=True, null=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("source_snapshot", models.JSONField(blank=True, default=dict)),
                ("is_manual", models.BooleanField(default=False)),
                ("pilot_revision", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assigned_to", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assigned_wms_new_returns", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_wms_new_returns", to=settings.AUTH_USER_MODEL)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="returns", to="wms_new.wmsneworder")),
            ],
            options={
                "db_table": "wms_new_return",
                "ordering": ("-source_created_at", "-id"),
            },
        ),
    ]
