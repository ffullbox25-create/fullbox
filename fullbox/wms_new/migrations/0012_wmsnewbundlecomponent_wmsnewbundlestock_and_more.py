import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sklad", "0011_live_snapshot_query_indexes"),
        ("wms_new", "0011_wmsnewreturn"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WmsNewBundleComponent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("bundle", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bundle_components", to="wms_new.wmsnewproduct")),
                ("component", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="used_in_bundles", to="wms_new.wmsnewproduct")),
            ],
            options={"db_table": "wms_new_bundle_component", "ordering": ("bundle_id", "id"), "constraints": [models.UniqueConstraint(fields=("bundle", "component"), name="uniq_wms_new_bundle_component")]},
        ),
        migrations.CreateModel(
            name="WmsNewBundleStock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("bundle", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bundle_stocks", to="wms_new.wmsnewproduct")),
                ("location", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="wms_new_bundle_stocks", to="sklad.warehouselocation")),
            ],
            options={"db_table": "wms_new_bundle_stock", "ordering": ("location_id", "bundle_id"), "constraints": [models.UniqueConstraint(fields=("bundle", "location"), name="uniq_wms_new_bundle_location")]},
        ),
        migrations.CreateModel(
            name="WmsNewBundleOperation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(choices=[("assemble", "Собрать набор"), ("disassemble", "Разобрать набор"), ("define", "Создать новый набор")], max_length=16)),
                ("quantity", models.PositiveIntegerField(default=1)),
                ("component_snapshot", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="wms_new_bundle_operations", to=settings.AUTH_USER_MODEL)),
                ("bundle", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bundle_operations", to="wms_new.wmsnewproduct")),
                ("location", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="wms_new_bundle_operations", to="sklad.warehouselocation")),
            ],
            options={"db_table": "wms_new_bundle_operation", "ordering": ("-created_at", "-id")},
        ),
    ]
