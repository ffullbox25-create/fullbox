from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sklad", "0009_warehousetemporarynomenclature_brand_color"),
    ]

    operations = [
        migrations.AddField(
            model_name="warehousecontainer",
            name="depth_mm",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="warehousecontainer",
            name="gross_weight_g",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="warehousecontainer",
            name="height_mm",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="warehousecontainer",
            name="width_mm",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
