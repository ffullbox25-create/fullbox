from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0042_pick_restock_pickup_item_scan"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fbscontrollerchecktote",
            name="tote",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="controller_check_uses",
                to="fbs.fbspickingcart",
            ),
        ),
    ]
