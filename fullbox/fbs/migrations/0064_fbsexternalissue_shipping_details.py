from django.db import migrations, models


LEGACY_PICKUP_DETAILS = {
    "delivery_type": "pickup",
    "delivery_type_label": "Самовывоз",
    "marketplace_id": None,
    "marketplace_name": "",
    "shipping_barcode": "",
    "supply_number": "",
    "supply_type": "",
    "supply_type_label": "",
    "destination_warehouse": "",
    "slot_date": "",
    "slot_time": "",
    "eta_date": "",
    "vehicle_type": "",
    "vehicle_type_label": "",
    "vehicle_number": "",
    "driver_phone": "",
}


def backfill_existing_issues(apps, schema_editor):
    issue_model = apps.get_model("fbs", "FbsExternalIssue")
    issue_model.objects.filter(shipping_details={}).update(
        shipping_details=LEGACY_PICKUP_DETAILS
    )


class Migration(migrations.Migration):
    dependencies = [("fbs", "0063_allow_whole_mixed_movement_boxes")]

    operations = [
        migrations.AddField(
            model_name="fbsexternalissue",
            name="shipping_details",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(backfill_existing_issues, migrations.RunPython.noop),
    ]
