"""Fill MoveRequest.process for rows created before the field existed.

The resolution rules are copied verbatim from
``reachtruck.models.resolve_move_request_process`` on purpose: a data migration
must keep behaving the same way even after that helper is edited later.  Only
rows with an empty ``process`` are touched, so re-running the migration is a
no-op.  ``context_type`` and ``context_id`` are never written.
"""
from django.db import migrations

FBS_CONTEXT_PREFIXES = ("fbs-movement:", "fbs-floor-movement:")
FBS_PAYLOAD_KEYS = (
    "fbs_movement_number",
    "fbs_movement_id",
    "fbs_replenishment_bridge_v1",
)
SHIPPING_PAYLOAD_KEYS = (
    "shipping_order_id",
    "shipping_order_pk",
    "otg_delivery_request_id",
)

BATCH_SIZE = 500


def resolve(context_type, context_id, destination_zone, payload):
    values = payload if isinstance(payload, dict) else {}
    context_type_key = str(context_type or "").strip().lower()
    context_id_key = str(context_id or "").strip().lower()
    destination_key = str(destination_zone or "").strip().upper()

    def has_value(key):
        return bool(str(values.get(key) or "").strip())

    if context_id_key.startswith(FBS_CONTEXT_PREFIXES) or any(
        values.get(key) for key in FBS_PAYLOAD_KEYS
    ):
        return "fbs"
    if context_type_key == "processing" or has_value("processing_order_id"):
        return "processing"
    # Shipping before receiving: a shipping pick keeps ``receiving_order_id`` in
    # its payload as provenance of the source pallet.  On production data 651 of
    # 664 shipping requests carry that key, so the other order mislabels them.
    if any(has_value(key) for key in SHIPPING_PAYLOAD_KEYS) or destination_key == "OTG":
        return "shipping"
    if context_type_key == "receiving" or has_value("receiving_order_id"):
        return "receiving"
    return "manual"


def backfill(apps, schema_editor):
    MoveRequest = apps.get_model("reachtruck", "MoveRequest")
    MoveTask = apps.get_model("reachtruck", "MoveTask")

    pending = []
    for request in (
        MoveRequest.objects.filter(process="")
        .only("id", "context_type", "context_id", "destination_zone")
        .iterator(chunk_size=BATCH_SIZE)
    ):
        first_task = (
            MoveTask.objects.filter(request_id=request.id)
            .order_by("id")
            .only("payload")
            .first()
        )
        payload = getattr(first_task, "payload", None)
        request.process = resolve(
            request.context_type,
            request.context_id,
            request.destination_zone,
            payload if isinstance(payload, dict) else {},
        )
        pending.append(request)
        if len(pending) >= BATCH_SIZE:
            MoveRequest.objects.bulk_update(pending, ["process"])
            pending = []
    if pending:
        MoveRequest.objects.bulk_update(pending, ["process"])


class Migration(migrations.Migration):

    dependencies = [
        ("reachtruck", "0003_moverequest_process"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
