from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0013_ozon_destinations_distribution"),
    ]

    operations = [
        migrations.AddField(
            model_name="shippingorder",
            name="requires_marking_scan",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Только для маркетплейс-отгрузки: маркированные позиции подбираются "
                    "и упаковываются парным сканом ШК товара и Data Matrix."
                ),
                verbose_name="Требуется сканирование Data Matrix",
            ),
        ),
    ]
