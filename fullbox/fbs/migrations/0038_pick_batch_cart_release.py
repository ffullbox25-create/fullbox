from django.db import migrations, models
from django.db.models import F, Max, Q


def backfill_cart_release(apps, schema_editor):
    PickBatch = apps.get_model("fbs", "FbsPickBatch")
    Allocation = apps.get_model("fbs", "FbsOrderStockAllocation")
    eligible = PickBatch.objects.filter(
        cart__isnull=False,
        picking_completed_at__isnull=False,
        verification_started_at__isnull=False,
        cart_released_at__isnull=True,
    ).order_by("id")
    for batch in eligible.iterator():
        allocations = Allocation.objects.filter(
            pick_task__batch_id=batch.id,
            qty_picked__gt=0,
        )
        if not allocations.exists():
            continue
        if allocations.filter(
            Q(verification_progress__isnull=True)
            | Q(verification_progress__qty_verified__lt=F("qty_picked"))
        ).exists():
            continue
        released_at = allocations.aggregate(
            released_at=Max("verification_progress__completed_at")
        )["released_at"]
        PickBatch.objects.filter(pk=batch.id, cart_released_at__isnull=True).update(
            cart_released_at=(
                released_at
                or batch.verification_started_at
                or batch.picking_completed_at
            )
        )


def clear_cart_release(apps, schema_editor):
    PickBatch = apps.get_model("fbs", "FbsPickBatch")
    PickBatch.objects.update(cart_released_at=None)


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0037_workstation_controller_shift"),
    ]

    operations = [
        migrations.AddField(
            model_name="fbspickbatch",
            name="cart_released_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_cart_release, clear_cart_release),
    ]
