from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("receiving_cz", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="receivingczunit",
            name="marking_code",
            field=models.TextField(),
        ),
        migrations.AddField(
            model_name="receivingczunit",
            name="source_shipping_order_number",
            field=models.CharField(blank=True, db_index=True, default="", max_length=32),
        ),
        migrations.AddConstraint(
            model_name="receivingczunit",
            constraint=models.UniqueConstraint(
                fields=("order_id", "marking_code"),
                name="uniq_receiving_cz_order_mark",
            ),
        ),
        migrations.AddConstraint(
            model_name="receivingczunit",
            constraint=models.UniqueConstraint(
                condition=~models.Q(source_shipping_order_number=""),
                fields=("source_shipping_order_number", "marking_code"),
                name="uniq_receiving_cz_source_mark",
            ),
        ),
    ]
