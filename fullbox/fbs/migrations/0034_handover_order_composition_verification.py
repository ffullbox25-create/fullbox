from django.db import migrations, models
import django.db.models.deletion
from django.db.models import F
from django.conf import settings


def preserve_existing_completed_handovers(apps, schema_editor):
    handover_order = apps.get_model("fbs", "FbsHandoverOrder")
    handover_order.objects.exclude(
        box__batch__profile__marketplace="wb",
        box__batch__status__in=("open", "ready"),
    ).update(
        verified_by_id=F("added_by_id"),
        verified_at=F("added_at"),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0033_fbsinventorysession_scan_mode"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="fbshandoverorder",
            name="verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fbshandoverorder",
            name="verified_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="verified_fbs_handover_orders",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="fbshandoverorder",
            name="verified_label",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="verified_fbs_handover_orders",
                to="fbs.fbsorderlabel",
            ),
        ),
        migrations.RunPython(
            preserve_existing_completed_handovers,
            migrations.RunPython.noop,
        ),
    ]
