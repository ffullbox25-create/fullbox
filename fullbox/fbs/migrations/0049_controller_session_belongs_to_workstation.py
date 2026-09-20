from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0048_workstation_capacity_7"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="fbscontrollersession",
            name="uniq_active_fbs_tote_session_controller",
        ),
    ]
