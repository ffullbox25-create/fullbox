from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0010_alter_shippingorderitem_comment"),
    ]

    operations = [
        migrations.AddField(
            model_name="shippingorder",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="child_orders",
                to="shipping.shippingorder",
                verbose_name="Родительская заявка (пакет Ozon)",
            ),
        ),
        migrations.AddIndex(
            model_name="shippingorder",
            index=models.Index(fields=["parent", "created_at"], name="shipping_sh_parent__7a1c2d_idx"),
        ),
    ]
