from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0030_fbs_cart_handover_flow"),
    ]

    operations = [
        migrations.AddField(
            model_name="fbsworkstation",
            name="active_handover_box",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="active_workstation",
                to="fbs.fbshandoverbox",
                verbose_name="Активный короб отгрузки",
            ),
        ),
    ]
