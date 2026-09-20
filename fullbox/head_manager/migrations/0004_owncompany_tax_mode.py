# Generated manually for billing contracts.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("head_manager", "0003_carrier_bank_address_carrier_bank_bik_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="owncompany",
            name="tax_mode",
            field=models.CharField(
                choices=[("vat", "С НДС"), ("no_vat", "Без НДС")],
                default="vat",
                max_length=16,
                verbose_name="Налоговый режим",
            ),
        ),
        migrations.AddField(
            model_name="owncompany",
            name="vat_rate",
            field=models.CharField(default="20", max_length=16, verbose_name="Ставка НДС"),
        ),
    ]
