from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
        ("wms_new", "0015_wmsnewdocument_wmsnewdocumentitem_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="WmsNewMarketplaceStock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("marketplace", models.CharField(choices=[("wb", "Wildberries"), ("ozon", "Ozon")], max_length=16)),
                ("credential_source_id", models.PositiveIntegerField(blank=True, null=True)),
                ("warehouse_code", models.CharField(max_length=128)),
                ("warehouse_name", models.CharField(max_length=255)),
                ("sku_code", models.CharField(max_length=128)),
                ("barcode", models.CharField(blank=True, max_length=128)),
                ("product_name", models.CharField(blank=True, max_length=255)),
                ("category", models.CharField(blank=True, max_length=128)),
                ("quantity", models.PositiveIntegerField(default=0)),
                ("fullbox_qty", models.PositiveIntegerField(default=0)),
                ("sales_30d", models.PositiveIntegerField(default=0)),
                ("days_cover", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("fetched_at", models.DateTimeField()),
                ("source_snapshot", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("agency", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="wms_new_marketplace_stocks", to="sku.agency")),
                ("product", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="marketplace_stocks", to="wms_new.wmsnewproduct")),
            ],
            options={
                "db_table": "wms_new_marketplace_stock",
                "ordering": ("agency_id", "marketplace", "warehouse_name", "product_name", "sku_code"),
            },
        ),
        migrations.AddConstraint(
            model_name="wmsnewmarketplacestock",
            constraint=models.UniqueConstraint(fields=("agency", "marketplace", "warehouse_code", "sku_code"), name="uniq_wms_new_market_stock_row"),
        ),
        migrations.AddIndex(
            model_name="wmsnewmarketplacestock",
            index=models.Index(fields=["agency", "marketplace", "fetched_at"], name="wms_new_mar_agency__d7e091_idx"),
        ),
        migrations.AddIndex(
            model_name="wmsnewmarketplacestock",
            index=models.Index(fields=["marketplace", "warehouse_code"], name="wms_new_mar_marketp_1780cb_idx"),
        ),
        migrations.AddIndex(
            model_name="wmsnewmarketplacestock",
            index=models.Index(fields=["quantity", "days_cover"], name="wms_new_mar_quantit_93718f_idx"),
        ),
    ]
