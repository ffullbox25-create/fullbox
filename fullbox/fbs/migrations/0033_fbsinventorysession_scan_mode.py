from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0032_pick_restock"),
    ]

    operations = [
        migrations.AddField(
            model_name="fbsinventorysession",
            name="scan_mode",
            field=models.CharField(
                choices=[("kiz", "С КИЗами"), ("barcode", "Без КИЗов")],
                default="barcode",
                max_length=16,
            ),
        ),
    ]
