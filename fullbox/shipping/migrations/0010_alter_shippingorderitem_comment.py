from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0009_shippingtransportnote"),
    ]

    operations = [
        migrations.AlterField(
            model_name="shippingorderitem",
            name="comment",
            field=models.TextField(blank=True, verbose_name="Комментарий"),
        ),
    ]
