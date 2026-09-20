from django.conf import settings
from django.db import migrations, models
from django.db.models import F
import django.db.models.deletion


def mark_scanned_used(apps, schema_editor):
    MarkingCode = apps.get_model("marking", "MarkingCode")
    MarkingCode.objects.filter(source="scan", used_at__isnull=True).update(
        used_at=F("created_at"),
        used_by=F("created_by"),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("marking", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="markingcode",
            name="used_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Использован"),
        ),
        migrations.AddField(
            model_name="markingcode",
            name="used_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="used_marking_codes",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Использовал",
            ),
        ),
        migrations.RunPython(mark_scanned_used, migrations.RunPython.noop),
    ]
