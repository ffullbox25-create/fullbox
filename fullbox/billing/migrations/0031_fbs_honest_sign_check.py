from datetime import date
from decimal import Decimal

from django.db import migrations, models


KEYZI_CLIENT_ID = 2951
KEYZI_INN = "7203566429"


def seed_keyzi_chz_check(apps, schema_editor):
    Agency = apps.get_model("sku", "Agency")
    BillingService = apps.get_model("billing", "BillingService")
    FbsClientRate = apps.get_model("billing", "FbsClientRate")
    TariffCategory = apps.get_model("billing", "TariffCategory")

    category, _ = TariffCategory.objects.update_or_create(
        code="fbs_shipping_items",
        defaults={
            "name": "FBS · Отгрузка по артикулам",
            "sort_order": 104,
            "is_active": True,
        },
    )
    BillingService.objects.update_or_create(
        code="fbs_honest_sign_check",
        defaults={
            "name": "FBS · Проверка ЧЗ",
            "unit": "шт",
            "vat_rate": "5",
            "category": category,
            "description": (
                "Технический контроль уникального подтверждённого КИЗ. "
                "Повторные и неуспешные сканы, а также проверки сверх "
                "фактического количества товара не начисляются."
            ),
            "sort_order": 1050,
            "used_in_billing": True,
            "is_active": True,
        },
    )

    client = Agency.objects.filter(pk=KEYZI_CLIENT_ID, inn=KEYZI_INN).first()
    if client is None:
        return
    FbsClientRate.objects.update_or_create(
        client_id=client.pk,
        operation="chz_check",
        liters_from=Decimal("0"),
        liters_to=None,
        valid_from=date(2026, 8, 1),
        defaults={
            "price": Decimal("5"),
            "unit": "шт",
            "vat_rate": "5",
            "vat_type": "vat_extra",
            "is_active": True,
            "comment": (
                "Проверка ЧЗ: уникальный подтверждённый КИЗ; "
                "только ООО «КЕЙЗИ», согласовано 09.09.2026"
            ),
        },
    )


def unseed_keyzi_chz_check(apps, schema_editor):
    BillingService = apps.get_model("billing", "BillingService")
    FbsClientRate = apps.get_model("billing", "FbsClientRate")
    FbsClientRate.objects.filter(
        client_id=KEYZI_CLIENT_ID,
        operation="chz_check",
        liters_from=Decimal("0"),
        liters_to__isnull=True,
        valid_from=date(2026, 8, 1),
        price=Decimal("5"),
    ).delete()
    BillingService.objects.filter(code="fbs_honest_sign_check").delete()


class Migration(migrations.Migration):
    dependencies = [("billing", "0030_fbs_client_rates")]

    operations = [
        migrations.AlterField(
            model_name="fbsclientrate",
            name="operation",
            field=models.CharField(
                choices=[
                    ("receiving", "Приемка"),
                    ("picking", "Подбор"),
                    ("marking", "Маркировка"),
                    ("chz_check", "Проверка ЧЗ"),
                    ("shipping", "Отгрузка FBS"),
                    ("storage", "Хранение"),
                ],
                max_length=16,
                verbose_name="Операция FBS",
            ),
        ),
        migrations.RunPython(seed_keyzi_chz_check, unseed_keyzi_chz_check),
    ]
