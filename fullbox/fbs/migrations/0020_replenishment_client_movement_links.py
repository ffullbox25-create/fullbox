import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fbs", "0019_fbsclientmovementrequest_and_lines"),
    ]

    operations = [
        migrations.AddField(
            model_name="fbsreplenishmentplan",
            name="client_movement_request",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replenishment_plans",
                to="fbs.fbsclientmovementrequest",
            ),
        ),
        migrations.AddField(
            model_name="fbsreplenishmentline",
            name="client_movement_line",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replenishment_lines",
                to="fbs.fbsclientmovementrequestline",
            ),
        ),
    ]
