from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sklad", "0004_stockpalletstate_sku_ref"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockpalletstate",
            name="available_qty",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="stockpalletstate",
            name="processing_reserved_qty",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="stockpalletstate",
            name="shipping_reserved_qty",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
