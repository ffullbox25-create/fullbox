from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("marking", "0003_markingcode_box_barcode"),
    ]

    operations = [
        migrations.AddField(
            model_name="markingcode",
            name="printed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Напечатан"),
        ),
        migrations.AddField(
            model_name="markingcode",
            name="printed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="printed_marking_codes",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Напечатал",
            ),
        ),
    ]
