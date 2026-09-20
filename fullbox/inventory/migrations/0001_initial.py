import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("sklad", "0010_warehousecontainer_dimensions_weight"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
    ]

    operations = [
        migrations.CreateModel(
            name="Inventory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("inventory_type", models.CharField(choices=[("full", "Полная"), ("by_partner", "По партнеру"), ("by_goods", "По товару"), ("by_places", "По местам")], max_length=16)),
                ("status", models.CharField(choices=[("created", "Создана"), ("pending", "Передана в работу"), ("in_progress", "В работе"), ("completed", "Завершена"), ("canceled", "Отменена")], default="created", max_length=16)),
                ("comment", models.TextField(blank=True)),
                ("performed_by_name", models.CharField(blank=True, max_length=255)),
                ("transferred_at", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("canceled_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("agency", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="inventories", to="sku.agency")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_inventories", to=settings.AUTH_USER_MODEL)),
                ("sku", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="inventories", to="sku.sku")),
            ],
            options={
                "verbose_name": "Инвентаризация",
                "verbose_name_plural": "Инвентаризации",
                "db_table": "inventory_inventory",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="InventoryLocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("planned_qty", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("inventory", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="scope_locations", to="inventory.inventory")),
                ("location", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_scopes", to="sklad.warehouselocation")),
            ],
            options={
                "verbose_name": "Место инвентаризации",
                "verbose_name_plural": "Места инвентаризации",
                "db_table": "inventory_location",
                "ordering": ["location__zone_code", "location__row_no", "location__section_no", "location__tier_no", "location__cell_no"],
            },
        ),
        migrations.CreateModel(
            name="InventoryLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sku_code", models.CharField(max_length=64)),
                ("name", models.CharField(blank=True, max_length=255)),
                ("size", models.CharField(blank=True, max_length=64)),
                ("barcode", models.CharField(blank=True, max_length=64)),
                ("goods_type", models.CharField(blank=True, max_length=64)),
                ("planned_qty", models.PositiveIntegerField(default=0)),
                ("actual_qty", models.PositiveIntegerField(blank=True, null=True)),
                ("source_snapshot_ids", models.JSONField(blank=True, default=list)),
                ("counted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("agency", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_lines", to="sku.agency")),
                ("counted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="counted_inventory_lines", to=settings.AUTH_USER_MODEL)),
                ("inventory", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="inventory.inventory")),
                ("location", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_lines", to="sklad.warehouselocation")),
                ("sku_ref", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="inventory_lines", to="sku.sku")),
            ],
            options={
                "verbose_name": "Строка инвентаризации",
                "verbose_name_plural": "Строки инвентаризации",
                "db_table": "inventory_line",
                "ordering": ["location_id", "agency_id", "sku_code", "size", "barcode", "id"],
            },
        ),
        migrations.AddIndex(model_name="inventory", index=models.Index(fields=["status", "created_at"], name="inventory_i_status_3f1d72_idx")),
        migrations.AddIndex(model_name="inventory", index=models.Index(fields=["inventory_type", "status"], name="inventory_i_invento_8818db_idx")),
        migrations.AddIndex(model_name="inventory", index=models.Index(fields=["agency", "status"], name="inventory_i_agency__a75ef3_idx")),
        migrations.AddIndex(model_name="inventorylocation", index=models.Index(fields=["inventory", "location"], name="inventory_l_invento_f2c15b_idx")),
        migrations.AddConstraint(model_name="inventorylocation", constraint=models.UniqueConstraint(fields=("inventory", "location"), name="uniq_inventory_location")),
        migrations.AddIndex(model_name="inventoryline", index=models.Index(fields=["inventory", "location"], name="inventory_l_invento_9f2d99_idx")),
        migrations.AddIndex(model_name="inventoryline", index=models.Index(fields=["inventory", "agency", "sku_code"], name="inventory_l_invento_27903d_idx")),
        migrations.AddIndex(model_name="inventoryline", index=models.Index(fields=["barcode"], name="inventory_l_barcode_d80e52_idx")),
    ]
