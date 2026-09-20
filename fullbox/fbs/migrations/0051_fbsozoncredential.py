from django.db import migrations, models
import django.db.models.deletion


def copy_existing_ozon_credentials(apps, schema_editor):
    FbsOzonCredential = apps.get_model("fbs", "FbsOzonCredential")
    MarketCredential = apps.get_model("sku", "MarketCredential")
    credentials = (
        MarketCredential.objects.select_related("market")
        .filter(market__name__icontains="OZON")
        .order_by("agency_id", "id")
    )
    seen_agencies = set()
    for credential in credentials.iterator():
        if credential.agency_id in seen_agencies:
            continue
        client_id = str(credential.client_id or "").strip()
        api_key = str(credential.market_key or "").strip()
        if not client_id or not api_key:
            continue
        FbsOzonCredential.objects.create(
            agency_id=credential.agency_id,
            slot=1,
            client_id=client_id,
            api_key=api_key,
        )
        seen_agencies.add(credential.agency_id)


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0050_fbshandoverbatch_dispatch_location"),
        ("sku", "0002_agency_color_market_sku_additional_name_sku_brand_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="FbsOzonCredential",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slot", models.PositiveSmallIntegerField(choices=[(1, "Кабинет 1"), (2, "Кабинет 2"), (3, "Кабинет 3")], verbose_name="Номер кабинета")),
                ("client_id", models.CharField(max_length=128, verbose_name="Client ID Ozon")),
                ("api_key", models.TextField(verbose_name="API-ключ Ozon")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("agency", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fbs_ozon_credentials", to="sku.agency", verbose_name="Клиент")),
            ],
            options={
                "verbose_name": "Реквизиты Ozon FBS",
                "verbose_name_plural": "Реквизиты Ozon FBS",
                "db_table": "fbs_ozon_credential",
                "ordering": ["agency_id", "slot", "id"],
                "constraints": [
                    models.UniqueConstraint(fields=("agency", "slot"), name="uniq_fbs_ozon_credential_slot"),
                    models.UniqueConstraint(fields=("agency", "client_id"), name="uniq_fbs_ozon_credential_client_id"),
                    models.CheckConstraint(condition=models.Q(("slot__gte", 1), ("slot__lte", 3)), name="fbs_ozon_credential_slot_1_3"),
                ],
            },
        ),
        migrations.RunPython(copy_existing_ozon_credentials, migrations.RunPython.noop),
    ]
