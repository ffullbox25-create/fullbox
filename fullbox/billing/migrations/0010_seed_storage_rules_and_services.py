from decimal import Decimal

from django.db import migrations


STORAGE_SERVICES = (
    ("storage_pallet_day", "Хранение палеты/день", "пал."),
    ("storage_pallet_month", "Хранение палеты/месяц", "пал."),
    ("storage_liter_day", "Хранение, литр/сутки", "л"),
    ("storage_liter_month", "Хранение, литр/месяц", "л"),
    ("storage_m3_day", "Хранение, м³/сутки", "м3"),
    ("storage_m3_month", "Хранение, м³/месяц", "м3"),
)


def seed_forward(apps, schema_editor):
    BillingService = apps.get_model("billing", "BillingService")
    ClientTariffVersion = apps.get_model("billing", "ClientTariffVersion")
    StorageCalculationRule = apps.get_model("billing", "StorageCalculationRule")

    for code, name, unit in STORAGE_SERVICES:
        BillingService.objects.update_or_create(
            code=code,
            defaults={"name": name, "unit": unit, "vat_rate": "20", "is_active": True},
        )

    for version in ClientTariffVersion.objects.all().iterator():
        StorageCalculationRule.objects.get_or_create(
            tariff_version_id=version.id,
            defaults={
                "billing_mode": "pallet_day",
                "month_mode": "calendar_prorate",
                "day_counting": "include_both",
                "free_period_type": "none",
                "free_period_value": Decimal("0"),
                "volume_level": "pallet",
                "space_coefficient_default": Decimal("1.000000"),
                "rounding_mode": "none",
                "min_billable_volume": Decimal("0"),
                "min_amount_day": Decimal("0"),
                "min_amount_month": Decimal("0"),
                "missing_dims_policy": "skip",
                "snapshot_hour": 23,
                "timezone_name": "Europe/Moscow",
            },
        )


def seed_backward(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0009_storage_billing_module"),
    ]

    operations = [
        migrations.RunPython(seed_forward, seed_backward),
    ]
