from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("fbs", "0056_fbs_wave_policy"),
    ]

    operations = [
        migrations.CreateModel(
            name="FbsClientStockPolicy",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "safety_stock_qty",
                    models.PositiveIntegerField(
                        default=0,
                        verbose_name="Страховой остаток на каждый SKU",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agency",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fbs_stock_policy",
                        to="sku.agency",
                        verbose_name="Клиент",
                    ),
                ),
            ],
            options={
                "verbose_name": "Страховой остаток FBS",
                "verbose_name_plural": "Страховые остатки FBS",
                "db_table": "fbs_client_stock_policy",
                "ordering": ["agency_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="fbsclientstockpolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(("safety_stock_qty__lte", 1000000)),
                name="fbs_stock_safety_qty_lte_1000000",
            ),
        ),
    ]
