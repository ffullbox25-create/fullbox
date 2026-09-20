from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marking", "0002_markingcode_used_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="markingcode",
            name="box_barcode",
            field=models.CharField(blank=True, max_length=128, verbose_name="Штрихкод короба"),
        ),
    ]
