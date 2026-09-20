from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("wms_new", "0008_wmsnewwave_source_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="WmsNewAssemblySession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("place_code", models.CharField(blank=True, max_length=128)),
                ("place_name", models.CharField(max_length=128)),
                ("status", models.CharField(choices=[("active", "В работе"), ("completed", "Завершена"), ("cancelled", "Отменена")], default="active", max_length=16)),
                ("planned_items", models.PositiveIntegerField(default=0)),
                ("assembled_items", models.PositiveIntegerField(default=0)),
                ("problem_items", models.PositiveIntegerField(default=0)),
                ("source_snapshot", models.JSONField(blank=True, default=dict)),
                ("pilot_revision", models.PositiveIntegerField(default=0)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("completed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="completed_wms_new_assembly_sessions", to=settings.AUTH_USER_MODEL)),
                ("current_order", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="current_assembly_sessions", to="wms_new.wmsneworder")),
                ("started_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="started_wms_new_assembly_sessions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "wms_new_assembly_session", "ordering": ("-started_at", "-id")},
        ),
        migrations.CreateModel(
            name="WmsNewAssemblyLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "Ожидает товар"), ("awaiting_order", "Ожидает этикетку заказа"), ("assembled", "Собран"), ("problem", "Проблема")], default="pending", max_length=24)),
                ("planned_quantity", models.PositiveIntegerField(default=1)),
                ("assembled_quantity", models.PositiveIntegerField(default=0)),
                ("requires_order_scan", models.BooleanField(default=True)),
                ("product_scan", models.CharField(blank=True, max_length=255)),
                ("order_scan", models.CharField(blank=True, max_length=255)),
                ("extra_data", models.JSONField(blank=True, default=dict)),
                ("problem_place", models.CharField(blank=True, max_length=128)),
                ("problem_reason", models.TextField(blank=True)),
                ("assembled_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assembled_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assembled_wms_new_lines", to=settings.AUTH_USER_MODEL)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assembly_lines", to="wms_new.wmsneworder")),
                ("order_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assembly_lines", to="wms_new.wmsneworderitem")),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="wms_new.wmsnewassemblysession")),
                ("wave", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assembly_lines", to="wms_new.wmsnewwave")),
            ],
            options={"db_table": "wms_new_assembly_line", "ordering": ("id",)},
        ),
        migrations.AddConstraint(
            model_name="wmsnewassemblyline",
            constraint=models.UniqueConstraint(fields=("session", "order_item"), name="uniq_wms_new_assembly_session_item"),
        ),
    ]
