from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0043_controller_check_tote_logical_flow"),
    ]

    operations = [
        migrations.AddField(
            model_name="fbscontrollersession",
            name="problem_tote",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="problem_controller_sessions",
                to="fbs.fbspickingcart",
            ),
        ),
        migrations.AddField(
            model_name="fbscontrollersession",
            name="canceled_tote",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="canceled_controller_sessions",
                to="fbs.fbspickingcart",
            ),
        ),
        migrations.AddField(
            model_name="fbspickrestockrequest",
            name="source_tote",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="pick_restock_requests",
                to="fbs.fbspickingcart",
            ),
        ),
        migrations.AddField(
            model_name="fbspickrestockrequest",
            name="quarantine_box",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="quarantine_pick_restock_requests",
                to="fbs.fbsbox",
            ),
        ),
    ]
