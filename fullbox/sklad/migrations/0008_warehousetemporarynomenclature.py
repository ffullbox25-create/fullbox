from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
        ("sklad", "0007_warehouse_core_models"),
    ]

    operations = [
        migrations.CreateModel(
            name="WarehouseTemporaryNomenclature",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("identity_key", models.CharField(max_length=512)),
                ("item_code", models.CharField(blank=True, max_length=64, verbose_name="Временный артикул")),
                ("name", models.CharField(max_length=255, verbose_name="Наименование")),
                ("size", models.CharField(blank=True, max_length=64, verbose_name="Размер")),
                ("barcode", models.CharField(blank=True, max_length=64, verbose_name="Штрихкод")),
                ("goods_type", models.CharField(blank=True, default="op", max_length=64, verbose_name="Тип товара")),
                ("first_context_type", models.CharField(blank=True, max_length=32)),
                ("first_context_id", models.CharField(blank=True, max_length=64)),
                ("last_context_type", models.CharField(blank=True, max_length=32)),
                ("last_context_id", models.CharField(blank=True, max_length=64)),
                ("normalized_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agency",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="warehouse_temporary_nomenclature",
                        to="sku.agency",
                        verbose_name="Клиент",
                    ),
                ),
                (
                    "normalized_sku_ref",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="temporary_nomenclature_sources",
                        to="sku.sku",
                        verbose_name="Нормализованный SKU",
                    ),
                ),
            ],
            options={
                "verbose_name": "Временная номенклатура склада",
                "verbose_name_plural": "Временная номенклатура склада",
                "db_table": "warehouse_temporary_nomenclature",
                "ordering": ["item_code", "name", "size", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="warehousetemporarynomenclature",
            index=models.Index(fields=["agency", "item_code"], name="warehouse_t_agency__ff7026_idx"),
        ),
        migrations.AddIndex(
            model_name="warehousetemporarynomenclature",
            index=models.Index(fields=["agency", "normalized_at"], name="warehouse_t_agency__3e5da6_idx"),
        ),
        migrations.AddConstraint(
            model_name="warehousetemporarynomenclature",
            constraint=models.UniqueConstraint(
                fields=("agency", "identity_key"),
                name="uniq_warehouse_temp_nomenclature_identity",
            ),
        ),
    ]
