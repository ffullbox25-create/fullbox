from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reachtruck_inventory", "0002_inventory_task_lease_and_discrepancy"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventorytask",
            name="planned_box_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="inventorytask",
            name="actual_box_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="inventorytask",
            name="planned_box_codes",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="inventorytask",
            name="planned_pallet_codes",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
