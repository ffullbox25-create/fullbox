import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("reachtruck_inventory", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventorytask",
            name="last_activity_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="inventorytask",
            name="lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="inventorytask",
            name="counted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="inventorytask",
            name="discrepancy_pallet_codes",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="inventorytask",
            name="discrepancy_box_codes",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="inventorytask",
            name="discrepancy_reported_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reported_inventory_discrepancies",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="inventorytask",
            name="discrepancy_reported_by_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="inventorytask",
            name="discrepancy_reported_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="inventorytask",
            index=models.Index(
                fields=["status", "lease_expires_at"],
                name="rt_inv_status_lease_idx",
            ),
        ),
    ]
