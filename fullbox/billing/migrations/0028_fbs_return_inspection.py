from django.db import migrations


def seed_fbs_return_inspection(apps, schema_editor):
    BillingService = apps.get_model("billing", "BillingService")
    TariffCategory = apps.get_model("billing", "TariffCategory")
    category, _ = TariffCategory.objects.update_or_create(
        code="fbs_returns",
        defaults={
            "name": "FBS · Возвраты",
            "sort_order": 106,
            "is_active": True,
        },
    )
    BillingService.objects.update_or_create(
        code="fbs_return_inspection",
        defaults={
            "name": "FBS · Осмотр возврата",
            "unit": "шт",
            "category": category,
            "sort_order": 1100,
            "used_in_billing": True,
            "is_active": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("billing", "0027_fbs_billing_contour")]

    operations = [
        migrations.RunPython(
            seed_fbs_return_inspection,
            migrations.RunPython.noop,
        )
    ]
