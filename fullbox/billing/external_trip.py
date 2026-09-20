from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import BillingApplication
from .warehouse_services import sync_logistics_trip_facts_to_billing


@transaction.atomic
def sync_completed_external_trip(trip_or_id, *, user=None) -> BillingApplication | None:
    from logistics.models import LogisticsTrip

    trip_id = getattr(trip_or_id, "pk", trip_or_id)
    trip = (
        LogisticsTrip.objects.select_related("external_details__client", "assigned_logistician")
        .filter(pk=trip_id, trip_kind=LogisticsTrip.KIND_EXTERNAL)
        .first()
    )
    if (
        trip is None
        or trip.status != LogisticsTrip.STATUS_COMPLETED
        or trip.driver_status != LogisticsTrip.DRIVER_STATUS_DELIVERED
    ):
        return None
    applications = sync_logistics_trip_facts_to_billing(trip=trip, user=user)
    return applications[0] if applications else None


def sync_completed_external_trips(*, limit: int = 5000, user=None) -> dict[str, int]:
    from logistics.models import LogisticsTrip

    trip_ids = list(
        LogisticsTrip.objects.filter(
            trip_kind=LogisticsTrip.KIND_EXTERNAL,
            status=LogisticsTrip.STATUS_COMPLETED,
            driver_status=LogisticsTrip.DRIVER_STATUS_DELIVERED,
        )
        .order_by("-closed_at", "-id")
        .values_list("id", flat=True)[:limit]
    )
    created = 0
    updated = 0
    skipped = 0
    for trip_id in trip_ids:
        before = BillingApplication.objects.filter(
            application_type=BillingApplication.TYPE_LOGISTICS,
            source_payload__trip_id=trip_id,
        ).exists()
        try:
            application = sync_completed_external_trip(trip_id, user=user)
        except ValidationError:
            application = None
        if application is None:
            skipped += 1
        elif before:
            updated += 1
        else:
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}
