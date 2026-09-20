# Generated manually for the receiving_cz app.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReceivingCzUnit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order_id", models.CharField(max_length=64)),
                ("sku_code", models.CharField(max_length=64)),
                ("name", models.CharField(blank=True, max_length=255)),
                ("size", models.CharField(blank=True, max_length=64)),
                ("barcode", models.CharField(max_length=128)),
                ("marking_code", models.TextField(unique=True)),
                ("box_code", models.CharField(max_length=128)),
                ("pallet_code", models.CharField(blank=True, max_length=128)),
                ("accepted_at", models.DateTimeField(auto_now_add=True)),
                (
                    "accepted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="receiving_cz_units",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "agency",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="receiving_cz_units",
                        to="sku.agency",
                    ),
                ),
                (
                    "sku",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="receiving_cz_units",
                        to="sku.sku",
                    ),
                ),
            ],
            options={
                "db_table": "receiving_cz_unit",
                "ordering": ["accepted_at", "id"],
                "indexes": [
                    models.Index(fields=["order_id", "sku_code", "size"], name="receiving_c_order_i_1c5e09_idx"),
                    models.Index(fields=["order_id", "box_code"], name="receiving_c_order_i_5bcc40_idx"),
                    models.Index(fields=["order_id", "pallet_code"], name="receiving_c_order_i_383014_idx"),
                ],
            },
        ),
    ]
