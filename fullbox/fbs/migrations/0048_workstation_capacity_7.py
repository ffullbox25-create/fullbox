from django.db import migrations, models
from django.db.models import Q


def set_all_workstations_to_seven(apps, schema_editor):
    workstation = apps.get_model("fbs", "FbsWorkstation")
    workstation.objects.update(max_parallel_waves=7)


def restore_workstations_to_four(apps, schema_editor):
    workstation = apps.get_model("fbs", "FbsWorkstation")
    workstation.objects.filter(max_parallel_waves__gt=4).update(max_parallel_waves=4)


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0047_fbs_movement_idempotency"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="fbsworkstation",
            name="fbs_workstation_parallel_waves_1_4",
        ),
        migrations.AlterField(
            model_name="fbsworkstation",
            name="max_parallel_waves",
            field=models.PositiveSmallIntegerField(
                default=7,
                verbose_name="Максимум параллельных волн",
            ),
        ),
        migrations.RunPython(
            set_all_workstations_to_seven,
            restore_workstations_to_four,
        ),
        migrations.AddConstraint(
            model_name="fbsworkstation",
            constraint=models.CheckConstraint(
                condition=Q(max_parallel_waves__gte=1) & Q(max_parallel_waves__lte=7),
                name="fbs_workstation_parallel_waves_1_7",
            ),
        ),
    ]
