from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sklad", "0008_warehousetemporarynomenclature"),
    ]

    operations = [
        migrations.AddField(
            model_name="warehousetemporarynomenclature",
            name="brand",
            field=models.CharField(blank=True, max_length=255, verbose_name="Бренд"),
        ),
        migrations.AddField(
            model_name="warehousetemporarynomenclature",
            name="color",
            field=models.CharField(blank=True, max_length=64, verbose_name="Цвет"),
        ),
    ]
