from __future__ import annotations

from decimal import Decimal

from .base import ChargeCandidate


def calculate(application) -> list[ChargeCandidate]:
    payload = application.source_payload or {}
    trip_id = str(payload.get("external_trip_id") or application.application_id or "").strip()
    trip_number = str(payload.get("trip_number") or application.application_id or "").strip()
    route = str(payload.get("route") or "").strip()
    return [
        ChargeCandidate(
            service_code="logistics_external_trip",
            quantity=Decimal("1"),
            operation_type="logistics_external_trip",
            operation_id=trip_id,
            source_key=f"logistics:external-trip:{trip_id}",
            comment=f"Внешний рейс {trip_number}{f' · {route}' if route else ''}",
        )
    ]
