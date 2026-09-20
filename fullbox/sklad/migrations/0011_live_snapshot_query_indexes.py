from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sklad", "0010_warehousecontainer_dimensions_weight"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="warehousestocksnapshot",
            index=models.Index(
                fields=["created_at", "id"],
                name="wh_live_created_idx",
                condition=models.Q(is_archived=False, qty__gt=0),
            ),
        ),
        migrations.AddIndex(
            model_name="warehousestocksnapshot",
            index=models.Index(
                fields=["agency", "created_at", "id"],
                name="wh_live_ag_created_idx",
                condition=models.Q(is_archived=False, qty__gt=0),
            ),
        ),
        migrations.AddIndex(
            model_name="warehousestocksnapshot",
            index=models.Index(
                fields=["container_code"],
                name="wh_live_code_idx",
                condition=models.Q(is_archived=False, qty__gt=0),
            ),
        ),
    ]
