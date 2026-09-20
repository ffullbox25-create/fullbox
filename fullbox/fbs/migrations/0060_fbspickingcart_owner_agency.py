from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0059_external_issues"),
    ]

    operations = [
        migrations.AddField(
            model_name="fbspickingcart",
            name="owner_agency",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="owned_fbs_picking_carts",
                to="sku.agency",
                verbose_name="Поставщик-владелец",
            ),
        ),
    ]
