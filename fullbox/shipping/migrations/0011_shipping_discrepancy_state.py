from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0010_alter_shippingorderitem_comment"),
    ]

    operations = [
        migrations.AddField(
            model_name="shippingorder",
            name="shipping_discrepancy_payload",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="shippingorder",
            name="shipping_discrepancy_status",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
