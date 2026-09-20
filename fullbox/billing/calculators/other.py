from __future__ import annotations

from decimal import Decimal

from .base import ChargeCandidate


def calculate(application) -> list[ChargeCandidate]:
    return [
        ChargeCandidate(
            service_code="extra_warehouse_operation",
            quantity=Decimal("1"),
            operation_type=application.application_type or "other",
            operation_id=application.application_id,
            source_key=f"{application.application_type or 'other'}:{application.application_id}:extra",
            comment=f"Прочая складская услуга по заявке {application.application_id}",
        )
    ]
