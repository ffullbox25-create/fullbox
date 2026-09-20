from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0025_external_trip_billing"),
    ]

    operations = [
        migrations.AlterField(
            model_name="warehouseservicefact",
            name="service",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="warehouse_service_facts",
                to="billing.billingservice",
                verbose_name="Услуга",
            ),
        ),
    ]
