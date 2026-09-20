from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0061_storekeeper_alert_control"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="fbspallet",
            name="uniq_active_fbs_pallet_per_cell",
        ),
    ]
