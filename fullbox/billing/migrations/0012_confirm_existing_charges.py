from django.db import migrations
from django.db.models import Q


def confirm_included_charges(apps, schema_editor):
    ApplicationCharge = apps.get_model("billing", "ApplicationCharge")
    ApplicationCharge.objects.filter(
        Q(is_included_in_act=True) | Q(is_included_in_invoice=True) | Q(is_confirmed=True)
    ).update(is_confirmed=True)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0011_storage_charge_period"),
    ]

    operations = [
        migrations.RunPython(confirm_included_charges, noop_reverse),
    ]
