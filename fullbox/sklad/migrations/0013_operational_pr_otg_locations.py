from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sklad", "0012_fbs_movement_reserve_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="warehouselocation",
            name="capacity_containers",
            field=models.PositiveIntegerField(
                default=0,
                help_text="0 означает, что вместимость места не ограничена.",
            ),
        ),
        migrations.AddField(
            model_name="warehouselocation",
            name="is_fbs_visible",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="warehouselocation",
            name="is_topology_visible",
            field=models.BooleanField(default=True),
        ),
        migrations.RemoveConstraint(
            model_name="warehouselocation",
            name="uniq_warehouse_location_slot",
        ),
        migrations.AddConstraint(
            model_name="warehouselocation",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_topology_visible=True),
                fields=(
                    "warehouse_code",
                    "zone_code",
                    "row_no",
                    "section_no",
                    "tier_no",
                    "cell_no",
                ),
                name="uniq_topology_location_slot",
            ),
        ),
        migrations.AddConstraint(
            model_name="warehouselocation",
            constraint=models.UniqueConstraint(
                condition=~models.Q(location_code=""),
                fields=("warehouse_code", "location_code"),
                name="uniq_warehouse_location_code",
            ),
        ),
        migrations.AddConstraint(
            model_name="warehouselocation",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(is_fbs_visible=False)
                    | models.Q(zone_code__in=["PR", "OTG"])
                ),
                name="warehouse_fbs_visible_pr_otg_only",
            ),
        ),
    ]
