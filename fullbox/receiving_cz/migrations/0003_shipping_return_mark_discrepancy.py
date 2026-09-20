from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("receiving_cz", "0002_shipping_return_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="receivingczunit",
            name="source_discrepancy_reason",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="receivingczunit",
            name="source_mark_verified",
            field=models.BooleanField(default=True),
        ),
    ]
