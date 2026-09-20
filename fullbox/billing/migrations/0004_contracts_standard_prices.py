# Generated manually for client tariff cabinet.

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def seed_standard_prices(apps, schema_editor):
    # Keep data source in runtime module because the PDF catalog is large and reused by commands.
    # Pass historical models: live BillingService later gains category_id (0007+).
    from billing.standard_price_catalog import seed_standard_prices as seed

    seed(
        service_model=apps.get_model("billing", "BillingService"),
        price_model=apps.get_model("billing", "StandardServicePrice"),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0003_service_catalog"),
        ("head_manager", "0004_owncompany_tax_mode"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
    ]

    operations = [
        migrations.CreateModel(
            name="StandardServicePrice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("section_code", models.CharField(max_length=16, verbose_name="Код раздела")),
                ("section_name", models.CharField(max_length=255, verbose_name="Раздел")),
                ("line_no", models.PositiveIntegerField(default=0, verbose_name="№ строки")),
                ("name", models.CharField(max_length=255, verbose_name="Название из прайса")),
                ("unit", models.CharField(default="шт", max_length=32, verbose_name="Единица")),
                ("base_price", models.DecimalField(blank=True, decimal_places=4, max_digits=14, null=True, verbose_name="Базовая цена")),
                ("price_note", models.CharField(blank=True, max_length=255, verbose_name="Примечание к цене")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активна")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "service",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="standard_price",
                        to="billing.billingservice",
                        verbose_name="Услуга",
                    ),
                ),
            ],
            options={
                "verbose_name": "Стандартная цена",
                "verbose_name_plural": "Стандартные цены",
                "ordering": ["section_code", "line_no", "service__code"],
            },
        ),
        migrations.CreateModel(
            name="ClientBillingContract",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "pricing_mode",
                    models.CharField(
                        choices=[
                            ("base", "Стандартный прайс"),
                            ("markup_5", "Стандартный прайс + 5%"),
                            ("individual", "Индивидуальные тарифы"),
                        ],
                        default="base",
                        max_length=32,
                        verbose_name="Режим тарификации",
                    ),
                ),
                ("valid_from", models.DateField(default=django.utils.timezone.localdate, verbose_name="Действует с")),
                ("valid_to", models.DateField(blank=True, null=True, verbose_name="Действует до")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активен")),
                ("comment", models.TextField(blank=True, verbose_name="Комментарий")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="billing_contracts",
                        to="sku.agency",
                        verbose_name="Клиент",
                    ),
                ),
                (
                    "own_company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="billing_contracts",
                        to="head_manager.owncompany",
                        verbose_name="Юрлицо FullBox",
                    ),
                ),
            ],
            options={
                "verbose_name": "Договор биллинга клиента",
                "verbose_name_plural": "Договоры биллинга клиентов",
                "ordering": ["client", "-valid_from", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="standardserviceprice",
            index=models.Index(fields=["section_code", "is_active"], name="billing_sta_section_2b0e59_idx"),
        ),
        migrations.AddIndex(
            model_name="standardserviceprice",
            index=models.Index(fields=["is_active"], name="billing_sta_is_acti_7d900e_idx"),
        ),
        migrations.AddIndex(
            model_name="clientbillingcontract",
            index=models.Index(fields=["client", "is_active", "valid_from"], name="billing_cli_client_6d34df_idx"),
        ),
        migrations.AddIndex(
            model_name="clientbillingcontract",
            index=models.Index(fields=["own_company", "is_active"], name="billing_cli_own_com_97a5b8_idx"),
        ),
        migrations.RunPython(seed_standard_prices, migrations.RunPython.noop),
    ]
