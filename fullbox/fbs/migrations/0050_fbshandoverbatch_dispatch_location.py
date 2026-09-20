from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0049_controller_session_belongs_to_workstation"),
        ("sklad", "0013_operational_pr_otg_locations"),
    ]

    operations = [
        migrations.AddField(
            model_name="fbshandoverbatch",
            name="dispatch_location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="fbs_handover_batches",
                to="sklad.warehouselocation",
            ),
        ),
        migrations.AddIndex(
            model_name="fbshandoverbatch",
            index=models.Index(
                fields=["dispatch_location", "status"],
                name="fbs_hand_dispatch_status_idx",
            ),
        ),
    ]
