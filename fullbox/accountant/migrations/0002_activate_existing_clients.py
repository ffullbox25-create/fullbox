from django.db import migrations


def forwards(apps, schema_editor):
    Agency = apps.get_model("sku", "Agency")
    ClientLifecycle = apps.get_model("accountant", "ClientLifecycle")
    for agency in Agency.objects.all().iterator():
        status = "archived" if agency.archived else "active"
        ClientLifecycle.objects.get_or_create(
            agency_id=agency.id,
            defaults={
                "status": status,
                "email_documents": agency.email or "",
                "email_notifications": agency.email or "",
            },
        )


def backwards(apps, schema_editor):
    # не удаляем lifecycle — безопасно для rollback схемы
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accountant", "0001_accountant_lifecycle_and_catalog"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
