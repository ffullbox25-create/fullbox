# Generated manually for agreed client tariff versions.

import django.db.models.deletion
import django.utils.timezone
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


def seed_catalog(apps, schema_editor):
    TariffCategory = apps.get_model("billing", "TariffCategory")
    TariffUnit = apps.get_model("billing", "TariffUnit")
    categories = (
        ("receiving", "Приёмка товара", 10),
        ("unloading", "Разгрузка", 20),
        ("processing", "Обработка товара", 30),
        ("marking", "Маркировка", 40),
        ("chestny_znak", "Честный знак", 50),
        ("packaging", "Упаковка", 60),
        ("materials", "Расходные материалы", 70),
        ("storage", "Хранение", 80),
        ("fbo_kitting", "Комплектация FBO", 90),
        ("fbs_processing", "Обработка FBS", 100),
        ("shipping", "Отгрузка", 110),
        ("logistics", "Логистика", 120),
        ("returns", "Возвраты", 130),
        ("extra", "Дополнительные услуги", 140),
        ("individual", "Индивидуальные услуги", 150),
    )
    units = (
        ("piece", "Штука", "шт."),
        ("box", "Короб", "кор."),
        ("pallet", "Палета", "пал."),
        ("order", "Заказ", "заказ"),
        ("request", "Заявка", "заявка"),
        ("supply", "Поставка", "поставка"),
        ("shipment", "Отгрузка", "отгр."),
        ("vehicle", "Машина", "маш."),
        ("trip", "Рейс", "рейс"),
        ("kg", "Килограмм", "кг"),
        ("liter", "Литр", "л"),
        ("m3", "Кубический метр", "м³"),
        ("m3_day", "Кубический метр в сутки", "м³/сутки"),
        ("pallet_day", "Палета в сутки", "пал./сутки"),
        ("box_day", "Короб в сутки", "кор./сутки"),
        ("hour", "Час", "час"),
        ("minute", "Минута", "мин."),
        ("sheet", "Лист", "лист"),
        ("label", "Этикетка", "этик."),
        ("marking_code", "Код маркировки", "КМ"),
        ("operation", "Операция", "операция"),
        ("fixed", "Фиксированная стоимость", "фикс."),
    )
    for code, name, sort_order in categories:
        TariffCategory.objects.update_or_create(
            code=code,
            defaults={"name": name, "sort_order": sort_order, "description": "", "is_active": True},
        )
    for code, name, short_name in units:
        TariffUnit.objects.update_or_create(
            code=code,
            defaults={"name": name, "short_name": short_name, "is_active": True},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0004_contracts_standard_prices"),
        ("employees", "0001_initial"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TariffCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.SlugField(max_length=64, unique=True, verbose_name="Код")),
                ("name", models.CharField(max_length=255, verbose_name="Название")),
                ("description", models.TextField(blank=True, verbose_name="Описание")),
                ("sort_order", models.PositiveIntegerField(default=100, verbose_name="Порядок")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активна")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Категория тарифа",
                "verbose_name_plural": "Категории тарифов",
                "ordering": ["sort_order", "name"],
            },
        ),
        migrations.CreateModel(
            name="TariffUnit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.SlugField(max_length=64, unique=True, verbose_name="Код")),
                ("name", models.CharField(max_length=255, verbose_name="Название")),
                ("short_name", models.CharField(max_length=64, verbose_name="Сокращение")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активна")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Единица расчёта тарифа",
                "verbose_name_plural": "Единицы расчёта тарифов",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="ClientTariffVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, verbose_name="Название редакции")),
                ("version_number", models.PositiveIntegerField(default=1, verbose_name="Номер версии")),
                ("status", models.CharField(choices=[("draft", "Черновик"), ("scheduled", "Будут действовать"), ("active", "Действуют"), ("expired", "Истекли"), ("archived", "Архив")], db_index=True, default="draft", max_length=32, verbose_name="Статус")),
                ("valid_from", models.DateField(verbose_name="Действует с")),
                ("valid_to", models.DateField(blank=True, null=True, verbose_name="Действует до")),
                ("vat_type", models.CharField(choices=[("vat", "С НДС"), ("no_vat", "Без НДС"), ("vat_extra", "НДС начисляется дополнительно")], default="vat", max_length=32, verbose_name="Система налогообложения")),
                ("currency", models.CharField(default="RUB", max_length=8, verbose_name="Валюта")),
                ("contract_number", models.CharField(blank=True, max_length=128, verbose_name="Номер договора")),
                ("contract_date", models.DateField(blank=True, null=True, verbose_name="Дата договора")),
                ("additional_agreement_number", models.CharField(blank=True, max_length=128, verbose_name="Номер доп. соглашения")),
                ("additional_agreement_date", models.DateField(blank=True, null=True, verbose_name="Дата доп. соглашения")),
                ("general_comment", models.TextField(blank=True, verbose_name="Общий комментарий")),
                ("approved_at", models.DateTimeField(blank=True, null=True, verbose_name="Дата утверждения")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="approved_client_tariff_versions", to=settings.AUTH_USER_MODEL, verbose_name="Кто утвердил")),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tariff_versions", to="sku.agency", verbose_name="Клиент")),
                ("contract", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="tariff_versions", to="billing.clientbillingcontract", verbose_name="Договор биллинга")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_client_tariff_versions", to=settings.AUTH_USER_MODEL, verbose_name="Кто создал")),
                ("manager", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="client_tariff_versions", to="employees.employee", verbose_name="Ответственный менеджер")),
            ],
            options={
                "verbose_name": "Редакция тарифов клиента",
                "verbose_name_plural": "Редакции тарифов клиентов",
                "ordering": ["client", "-valid_from", "-version_number"],
            },
        ),
        migrations.CreateModel(
            name="ClientTariffCondition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("condition_type", models.CharField(choices=[("free_storage", "Бесплатное хранение"), ("min_monthly", "Минимальный ежемесячный платёж"), ("materials", "Материалы"), ("urgent_coefficient", "Срочность"), ("volume_threshold", "Порог объёма"), ("delivery_separate", "Доставка отдельно"), ("min_order", "Минимальная стоимость заявки"), ("other", "Другое")], default="other", max_length=64, verbose_name="Тип условия")),
                ("name", models.CharField(max_length=255, verbose_name="Название")),
                ("description", models.TextField(blank=True, verbose_name="Описание")),
                ("value", models.CharField(blank=True, max_length=128, verbose_name="Значение")),
                ("valid_from", models.DateField(blank=True, null=True, verbose_name="Действует с")),
                ("valid_to", models.DateField(blank=True, null=True, verbose_name="Действует до")),
                ("sort_order", models.PositiveIntegerField(default=100, verbose_name="Порядок")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активно")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("tariff_version", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="conditions", to="billing.clienttariffversion", verbose_name="Редакция тарифов")),
                ("unit", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="tariff_conditions", to="billing.tariffunit", verbose_name="Единица")),
            ],
            options={
                "verbose_name": "Специальное условие тарифа",
                "verbose_name_plural": "Специальные условия тарифов",
                "ordering": ["sort_order", "name"],
            },
        ),
        migrations.CreateModel(
            name="ClientTariffItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("service_name", models.CharField(max_length=255, verbose_name="Название услуги в редакции")),
                ("description", models.TextField(blank=True, verbose_name="Описание")),
                ("price", models.DecimalField(decimal_places=4, max_digits=14, verbose_name="Цена")),
                ("minimum_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="Минимальная сумма")),
                ("minimum_quantity", models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True, verbose_name="Минимальное количество")),
                ("included_materials", models.TextField(blank=True, verbose_name="Материалы")),
                ("conditions", models.TextField(blank=True, verbose_name="Условия применения")),
                ("calculation_type", models.CharField(choices=[("by_unit", "За единицу"), ("fixed", "Фиксированная стоимость"), ("coefficient", "Коэффициент")], default="by_unit", max_length=32, verbose_name="Тип расчёта")),
                ("coefficient", models.DecimalField(decimal_places=4, default=Decimal("1.0000"), max_digits=8, verbose_name="Коэффициент")),
                ("sort_order", models.PositiveIntegerField(default=100, verbose_name="Порядок")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активен")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("category", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="tariff_items", to="billing.tariffcategory", verbose_name="Категория")),
                ("service", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="tariff_items", to="billing.billingservice", verbose_name="Услуга")),
                ("tariff_version", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="billing.clienttariffversion", verbose_name="Редакция тарифов")),
                ("unit", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="tariff_items", to="billing.tariffunit", verbose_name="Единица расчёта")),
            ],
            options={
                "verbose_name": "Позиция тарифов клиента",
                "verbose_name_plural": "Позиции тарифов клиентов",
                "ordering": ["category__sort_order", "sort_order", "service_name"],
            },
        ),
        migrations.AddIndex(model_name="tariffcategory", index=models.Index(fields=["is_active", "sort_order"], name="billing_tar_is_acti_1f90f3_idx")),
        migrations.AddIndex(model_name="tariffunit", index=models.Index(fields=["is_active", "code"], name="billing_tar_is_acti_e251f4_idx")),
        migrations.AddIndex(model_name="clienttariffversion", index=models.Index(fields=["client", "status"], name="billing_cli_client_646831_idx")),
        migrations.AddIndex(model_name="clienttariffversion", index=models.Index(fields=["client", "valid_from"], name="billing_cli_client_e16994_idx")),
        migrations.AddIndex(model_name="clienttariffversion", index=models.Index(fields=["client", "valid_to"], name="billing_cli_client_43ed6f_idx")),
        migrations.AddIndex(model_name="clienttariffversion", index=models.Index(fields=["status", "valid_from"], name="billing_cli_status_8e723f_idx")),
        migrations.AddConstraint(model_name="clienttariffversion", constraint=models.UniqueConstraint(fields=("client", "version_number"), name="uniq_client_tariff_version_number")),
        migrations.AddIndex(model_name="clienttariffcondition", index=models.Index(fields=["tariff_version", "is_active"], name="billing_cli_tariff_4ea078_idx")),
        migrations.AddIndex(model_name="clienttariffcondition", index=models.Index(fields=["condition_type", "is_active"], name="billing_cli_conditi_38fc69_idx")),
        migrations.AddIndex(model_name="clienttariffitem", index=models.Index(fields=["tariff_version", "is_active"], name="billing_cli_tariff_233bfb_idx")),
        migrations.AddIndex(model_name="clienttariffitem", index=models.Index(fields=["service", "is_active"], name="billing_cli_service_39b5d7_idx")),
        migrations.AddIndex(model_name="clienttariffitem", index=models.Index(fields=["category", "is_active"], name="billing_cli_categor_6e9af7_idx")),
        migrations.AddConstraint(model_name="clienttariffitem", constraint=models.UniqueConstraint(fields=("tariff_version", "service", "conditions"), name="uniq_client_tariff_item_service_condition")),
        migrations.RunPython(seed_catalog, migrations.RunPython.noop),
    ]
