from django.db import migrations, models
from django.db.models import OuterRef, Subquery
import django.db.models.deletion


def populate_barcode_agency(apps, schema_editor):
    SKU = apps.get_model("sku", "SKU")
    SKUBarcode = apps.get_model("sku", "SKUBarcode")
    agency_ids = SKU.objects.filter(pk=OuterRef("sku_id")).values("agency_id")[:1]
    SKUBarcode.objects.update(agency_id=Subquery(agency_ids))


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
    ]

    operations = [
        migrations.AddField(
            model_name="skubarcode",
            name="agency",
            field=models.ForeignKey(
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="sku_barcodes",
                to="sku.agency",
                verbose_name="Клиент",
            ),
        ),
        migrations.RunPython(populate_barcode_agency, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="skubarcode",
            name="value",
            field=models.CharField(max_length=64, verbose_name="Штрихкод"),
        ),
        migrations.AlterField(
            model_name="skubarcode",
            name="agency",
            field=models.ForeignKey(
                editable=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="sku_barcodes",
                to="sku.agency",
                verbose_name="Клиент",
            ),
        ),
        migrations.AddConstraint(
            model_name="skubarcode",
            constraint=models.UniqueConstraint(
                fields=("agency", "value"),
                name="uniq_sku_barcode_per_agency",
            ),
        ),
    ]
