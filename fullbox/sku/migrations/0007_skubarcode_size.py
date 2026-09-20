from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sku", "0006_agency_archived_alter_agency_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="skubarcode",
            name="size",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                verbose_name="Размер",
            ),
        ),
    ]
