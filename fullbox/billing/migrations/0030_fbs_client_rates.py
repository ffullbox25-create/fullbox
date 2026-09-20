from datetime import date
from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


PILOT_CLIENT_INNS = ("164509288615", "637722509860")
FBS_RATE_BANDS = {
    "receiving": (("0", "1", "2"), ("1", "2", "4"), ("2", "5", "6"), ("5", "10", "8"), ("10", "15", "10")),
    "picking": (("0", "1", "5"), ("1", "2", "6"), ("2", "5", "8"), ("5", "10", "10"), ("10", "15", "13")),
    "shipping": (("0", "1", "3.76"), ("1", "2", "7.52"), ("2", "5", "11.28"), ("5", "10", "31.96"), ("10", "15", "52.64")),
}


def seed_fbs_pilot_rates(apps, schema_editor):
    Agency = apps.get_model("sku", "Agency")
    BillingService = apps.get_model("billing", "BillingService")
    FbsClientRate = apps.get_model("billing", "FbsClientRate")
    TariffCategory = apps.get_model("billing", "TariffCategory")
    category, _ = TariffCategory.objects.update_or_create(
        code="fbs_shipping_items",
        defaults={"name": "FBS · Отгрузка по артикулам", "sort_order": 104, "is_active": True},
    )
    for code, name, unit, sort_order in (
        ("fbs_marking_label_58x40", "FBS · Маркировка ШК 58×40", "шт", 1045),
        ("fbs_shipping_item", "FBS · Отгрузка товара", "шт", 1055),
    ):
        BillingService.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "unit": unit,
                "category": category,
                "sort_order": sort_order,
                "used_in_billing": True,
                "is_active": True,
            },
        )
    for client in Agency.objects.filter(inn__in=PILOT_CLIENT_INNS):
        for operation, bands in FBS_RATE_BANDS.items():
            for lower, upper, price in bands:
                FbsClientRate.objects.update_or_create(
                    client_id=client.pk,
                    operation=operation,
                    liters_from=Decimal(lower),
                    liters_to=Decimal(upper),
                    valid_from=date(2026, 8, 1),
                    defaults={
                        "price": Decimal(price),
                        "unit": "шт",
                        "vat_rate": "5",
                        "vat_type": "vat_extra",
                        "is_active": True,
                        "comment": "КП-010826-002: НДС 5% сверху",
                    },
                )
        for operation, price, unit, comment in (
            ("marking", "5", "шт", "Маркировка ШК 58×40; КП-010826-002"),
            ("storage", "0.16", "л", "Хранение FBS, литр/сутки; КП-010826-002"),
        ):
            FbsClientRate.objects.update_or_create(
                client_id=client.pk,
                operation=operation,
                liters_from=Decimal("0"),
                liters_to=None,
                valid_from=date(2026, 8, 1),
                defaults={
                    "price": Decimal(price),
                    "unit": unit,
                    "vat_rate": "5",
                    "vat_type": "vat_extra",
                    "is_active": True,
                    "comment": comment,
                },
            )


class Migration(migrations.Migration):
    # Production billing contour currently ends at 0027. The local-only
    # return-inspection seed must not become an implicit deploy dependency.
    dependencies = [("billing", "0027_fbs_billing_contour")]

    operations = [
        migrations.CreateModel(
            name="FbsClientRate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("operation", models.CharField(choices=[("receiving", "Приемка"), ("picking", "Подбор"), ("marking", "Маркировка"), ("shipping", "Отгрузка FBS"), ("storage", "Хранение")], max_length=16, verbose_name="Операция FBS")),
                ("liters_from", models.DecimalField(decimal_places=3, default=Decimal("0"), max_digits=12, verbose_name="Больше, чем литров")),
                ("liters_to", models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True, verbose_name="До литров включительно")),
                ("price", models.DecimalField(decimal_places=4, max_digits=14, verbose_name="Цена за единицу")),
                ("unit", models.CharField(default="шт", max_length=16, verbose_name="Единица расчета")),
                ("valid_from", models.DateField(verbose_name="Действует с")),
                ("valid_to", models.DateField(blank=True, null=True, verbose_name="Действует до")),
                ("vat_rate", models.CharField(default="5", max_length=16, verbose_name="Ставка НДС")),
                ("vat_type", models.CharField(default="vat_extra", max_length=32, verbose_name="Режим НДС")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активна")),
                ("comment", models.CharField(blank=True, max_length=255, verbose_name="Комментарий")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fbs_client_rates", to="sku.agency", verbose_name="Клиент")),
            ],
            options={
                "verbose_name": "Ставка FBS клиента",
                "verbose_name_plural": "Ставки FBS клиентов",
                "ordering": ["client", "operation", "valid_from", "liters_from", "id"],
            },
        ),
        migrations.AddIndex(model_name="fbsclientrate", index=models.Index(fields=["client", "operation", "valid_from"], name="billing_fbs_client_oper_date_idx")),
        migrations.AddIndex(model_name="fbsclientrate", index=models.Index(fields=["client", "is_active"], name="billing_fbs_client_active_idx")),
        migrations.RunPython(seed_fbs_pilot_rates, migrations.RunPython.noop),
    ]
