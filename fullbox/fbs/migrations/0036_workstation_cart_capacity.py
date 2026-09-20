from django.db import migrations, models
from django.db.models import Q


def set_four_cart_capacity(apps, schema_editor):
    workstation = apps.get_model("fbs", "FbsWorkstation")
    workstation.objects.filter(max_parallel_waves=2).update(max_parallel_waves=4)


def restore_two_cart_capacity(apps, schema_editor):
    workstation = apps.get_model("fbs", "FbsWorkstation")
    workstation.objects.filter(max_parallel_waves__gt=2).update(max_parallel_waves=2)


class Migration(migrations.Migration):

    dependencies = [
        ("fbs", "0035_order_exclusion_and_single_restock"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="fbsworkstation",
            name="fbs_workstation_parallel_waves_1_2",
        ),
        migrations.AlterField(
            model_name="fbsworkstation",
            name="max_parallel_waves",
            field=models.PositiveSmallIntegerField(
                default=4,
                verbose_name="Максимум параллельных волн",
            ),
        ),
        migrations.AddConstraint(
            model_name="fbsworkstation",
            constraint=models.CheckConstraint(
                condition=Q(max_parallel_waves__gte=1) & Q(max_parallel_waves__lte=4),
                name="fbs_workstation_parallel_waves_1_4",
            ),
        ),
        migrations.RunPython(set_four_cart_capacity, restore_two_cart_capacity),
    ]
