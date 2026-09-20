from django.db import migrations, models


def forwards(apps, schema_editor):
    ClientLifecycle = apps.get_model("accountant", "ClientLifecycle")
    for lifecycle in ClientLifecycle.objects.select_related("agency").all():
        lifecycle.vat_type = "vat_extra" if getattr(lifecycle.agency, "use_nds", False) else "no_vat"
        lifecycle.save(update_fields=["vat_type"])


def backwards(apps, schema_editor):
    ClientLifecycle = apps.get_model("accountant", "ClientLifecycle")
    for lifecycle in ClientLifecycle.objects.select_related("agency").all():
        if getattr(lifecycle.agency, "use_nds", False) != (lifecycle.vat_type != "no_vat"):
            lifecycle.agency.use_nds = lifecycle.vat_type != "no_vat"
            lifecycle.agency.save(update_fields=["use_nds"])


class Migration(migrations.Migration):

    dependencies = [
        ("accountant", "0003_client_type_ao"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientlifecycle",
            name="vat_type",
            field=models.CharField(
                choices=[
                    ("no_vat", "Без НДС"),
                    ("vat", "НДС в том числе"),
                    ("vat_extra", "НДС сверху"),
                ],
                default="no_vat",
                max_length=32,
                verbose_name="НДС клиента",
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
