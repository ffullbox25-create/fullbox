# Generated manually for immutable invoice rendering.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0021_warehouse_service_fact_registry"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientinvoice",
            name="supplier_snapshot",
            field=models.JSONField(blank=True, default=dict, verbose_name="Снимок реквизитов поставщика"),
        ),
        migrations.AddField(
            model_name="clientinvoice",
            name="vat_rate_snapshot",
            field=models.CharField(blank=True, max_length=16, verbose_name="Ставка НДС в счёте"),
        ),
    ]
