from django.conf import settings
from django.db import migrations, models

import shipping.models


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0005_shippingorder_transit_address"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ShippingOrderAttachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to=shipping.models.shipping_attachment_upload_to, verbose_name="Файл")),
                ("uploaded_at", models.DateTimeField(auto_now_add=True, verbose_name="Загружен")),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="attachments",
                        to="shipping.shippingorder",
                        verbose_name="Заявка",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="shipping_order_attachments",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто загрузил",
                    ),
                ),
            ],
            options={
                "verbose_name": "Файл заявки на отгрузку",
                "verbose_name_plural": "Файлы заявок на отгрузку",
                "ordering": ["-uploaded_at"],
            },
        ),
    ]
