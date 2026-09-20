from django.db import migrations, models


def fill_basis_period(apps, schema_editor):
    Rule = apps.get_model("billing", "StorageCalculationRule")
    mapping = {
        "pallet_day": ("pallet", "day"),
        "pallet_week": ("pallet", "week"),
        "pallet_month": ("pallet", "month"),
        "liter_day": ("liter", "day"),
        "liter_week": ("liter", "week"),
        "liter_month": ("liter", "month"),
        "m3_day": ("m3", "day"),
        "m3_week": ("m3", "week"),
        "m3_month": ("m3", "month"),
        "custom": ("pallet", "day"),
    }
    for rule in Rule.objects.all().iterator():
        basis, period = mapping.get(rule.billing_mode or "pallet_day", ("pallet", "day"))
        rule.charge_basis = basis
        rule.charge_period = period
        rule.save(update_fields=["charge_basis", "charge_period"])


def seed_week_services(apps, schema_editor):
    BillingService = apps.get_model("billing", "BillingService")
    for code, name, unit in (
        ("storage_pallet_week", "Хранение палеты/неделя", "пал."),
        ("storage_liter_week", "Хранение, литр/неделя", "л"),
        ("storage_m3_week", "Хранение, м³/неделя", "м3"),
    ):
        BillingService.objects.update_or_create(
            code=code,
            defaults={"name": name, "unit": unit, "vat_rate": "20", "is_active": True},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0010_seed_storage_rules_and_services"),
    ]

    operations = [
        migrations.AddField(
            model_name="storagecalculationrule",
            name="charge_basis",
            field=models.CharField(
                choices=[("pallet", "Палетоместо"), ("liter", "Литр"), ("m3", "м³")],
                default="pallet",
                help_text="За что берётся цена: палета, литр или м³.",
                max_length=16,
                verbose_name="Единица тарифа",
            ),
        ),
        migrations.AddField(
            model_name="storagecalculationrule",
            name="charge_period",
            field=models.CharField(
                choices=[("day", "За сутки"), ("week", "За неделю"), ("month", "За месяц")],
                default="day",
                help_text="За какой период указана цена в тарифе: сутки, неделя или месяц.",
                max_length=16,
                verbose_name="Период тарифа",
            ),
        ),
        migrations.AlterField(
            model_name="storagecalculationrule",
            name="billing_mode",
            field=models.CharField(
                choices=[
                    ("liter_day", "За 1 литр в сутки"),
                    ("liter_week", "За 1 литр в неделю"),
                    ("liter_month", "За 1 литр в месяц"),
                    ("m3_day", "За 1 м³ в сутки"),
                    ("m3_week", "За 1 м³ в неделю"),
                    ("m3_month", "За 1 м³ в месяц"),
                    ("pallet_day", "За палетоместо в сутки"),
                    ("pallet_week", "За палетоместо в неделю"),
                    ("pallet_month", "За палетоместо в месяц"),
                    ("custom", "Индивидуальная схема"),
                ],
                default="pallet_day",
                max_length=32,
                verbose_name="Способ тарификации",
            ),
        ),
        migrations.RunPython(fill_basis_period, migrations.RunPython.noop),
        migrations.RunPython(seed_week_services, migrations.RunPython.noop),
    ]
