# Generated manually for multi-act invoices

import django.db.models.deletion
from django.db import migrations, models


def backfill_invoice_acts(apps, schema_editor):
    ClientInvoice = apps.get_model("billing", "ClientInvoice")
    ClientInvoiceAct = apps.get_model("billing", "ClientInvoiceAct")
    for invoice in ClientInvoice.objects.exclude(act_id=None).iterator():
        ClientInvoiceAct.objects.get_or_create(
            invoice_id=invoice.id,
            act_id=invoice.act_id,
            defaults={"sort_order": 0},
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0007_accountant_lifecycle_and_catalog"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClientInvoiceAct",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sort_order", models.PositiveIntegerField(default=0, verbose_name="Порядок")),
                (
                    "act",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invoice_links",
                        to="billing.billingact",
                        verbose_name="Акт",
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="invoice_acts",
                        to="billing.clientinvoice",
                        verbose_name="Счёт",
                    ),
                ),
            ],
            options={
                "verbose_name": "Акт в счёте",
                "verbose_name_plural": "Акты в счетах",
                "ordering": ["sort_order", "id"],
            },
        ),
        migrations.AddField(
            model_name="clientinvoice",
            name="linked_acts",
            field=models.ManyToManyField(
                blank=True,
                related_name="grouped_invoices",
                through="billing.ClientInvoiceAct",
                to="billing.billingact",
                verbose_name="Акты в счёте",
            ),
        ),
        migrations.AlterField(
            model_name="clientinvoice",
            name="act",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invoices",
                to="billing.billingact",
                verbose_name="Основной акт",
            ),
        ),
        migrations.AddConstraint(
            model_name="clientinvoiceact",
            constraint=models.UniqueConstraint(fields=("invoice", "act"), name="uniq_billing_invoice_act"),
        ),
        migrations.AddIndex(
            model_name="clientinvoiceact",
            index=models.Index(fields=["act"], name="billing_cli_act_id_7f0a8c_idx"),
        ),
        migrations.AddIndex(
            model_name="clientinvoiceact",
            index=models.Index(fields=["invoice", "sort_order"], name="billing_cli_invoice_8c1b2d_idx"),
        ),
        migrations.RunPython(backfill_invoice_acts, noop_reverse),
    ]
