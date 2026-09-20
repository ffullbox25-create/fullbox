from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
        ("shipping", "0011_shipping_discrepancy_state"),
    ]

    operations = [
        migrations.CreateModel(
            name="ShippingReservationUnit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "reserve_mode",
                    models.CharField(
                        choices=[
                            ("full_pallet", "Full pallet"),
                            ("pallet_quota", "Pallet quota"),
                            ("fixed_box", "Fixed box"),
                            ("loose_qty", "Loose qty"),
                        ],
                        default="loose_qty",
                        max_length=24,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("reserved", "Reserved"),
                            ("task_created", "Task created"),
                            ("picked", "Picked"),
                            ("in_otg", "In OTG"),
                            ("palletized", "Palletized"),
                            ("shipped", "Shipped"),
                            ("released", "Released"),
                            ("substituted", "Substituted"),
                        ],
                        default="reserved",
                        max_length=24,
                    ),
                ),
                ("sku_code", models.CharField(max_length=64)),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                ("size", models.CharField(blank=True, default="", max_length=64)),
                ("barcode", models.CharField(blank=True, default="", max_length=64)),
                ("goods_type", models.CharField(blank=True, default="gv", max_length=32)),
                ("qty_per_box", models.PositiveIntegerField(default=0)),
                ("boxes_required", models.PositiveIntegerField(default=0)),
                ("qty_required", models.PositiveIntegerField(default=0)),
                ("pallet_code", models.CharField(blank=True, default="", max_length=128)),
                ("box_code", models.CharField(blank=True, default="", max_length=128)),
                ("snapshot_id", models.PositiveIntegerField(blank=True, null=True)),
                ("source_location", models.CharField(blank=True, default="", max_length=255)),
                ("source_zone", models.CharField(blank=True, default="", max_length=32)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agency",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="shipping_reservation_units",
                        to="sku.agency",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_shipping_reservation_units",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reservation_units",
                        to="shipping.shippingorderitem",
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reservation_units",
                        to="shipping.shippingorder",
                    ),
                ),
                (
                    "released_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="released_shipping_reservation_units",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["order", "status"], name="shipping_sh_order__5c3341_idx"),
                    models.Index(fields=["agency", "status"], name="shipping_sh_agency__805bb6_idx"),
                    models.Index(fields=["pallet_code", "status"], name="shipping_sh_pallet__6fdc17_idx"),
                    models.Index(fields=["box_code", "status"], name="shipping_sh_box_cod_b364f4_idx"),
                    models.Index(fields=["snapshot_id"], name="shipping_sh_snapsho_75cf78_idx"),
                ],
            },
        ),
    ]
