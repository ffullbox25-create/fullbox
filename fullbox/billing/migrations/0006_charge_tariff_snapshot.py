from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0005_client_tariff_versions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="applicationcharge",
            name="tariff_price",
            field=models.DecimalField(
                blank=True, decimal_places=4, max_digits=14, null=True, verbose_name="Цена по согласованному тарифу"
            ),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="coefficient",
            field=models.DecimalField(decimal_places=4, default=Decimal("1.0000"), max_digits=8, verbose_name="Коэффициент"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="minimum_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="Минимальная сумма"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="client_tariff_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="charges",
                to="billing.clienttariffversion",
                verbose_name="Редакция тарифов",
            ),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="client_tariff_item",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="charges",
                to="billing.clienttariffitem",
                verbose_name="Позиция тарифа",
            ),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="service_name_snapshot",
            field=models.CharField(blank=True, max_length=255, verbose_name="Название услуги (снимок)"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="tariff_source_label",
            field=models.CharField(blank=True, max_length=255, verbose_name="Источник цены"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="tariff_basis",
            field=models.CharField(blank=True, max_length=255, verbose_name="Основание тарифа"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="is_manual_override",
            field=models.BooleanField(db_index=True, default=False, verbose_name="Цена изменена вручную"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="override_reason",
            field=models.TextField(blank=True, verbose_name="Причина ручного изменения цены"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="overridden_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="overridden_billing_charges",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Кто изменил цену",
            ),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="overridden_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Когда изменена цена"),
        ),
        migrations.AlterField(
            model_name="applicationcharge",
            name="tariff",
            field=models.DecimalField(decimal_places=4, max_digits=14, verbose_name="Применённая цена"),
        ),
    ]
