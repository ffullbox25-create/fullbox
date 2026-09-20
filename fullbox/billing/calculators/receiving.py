from __future__ import annotations

from decimal import Decimal

from .base import ChargeCandidate
from .utils import at_least_one, first_number, list_count, list_sum, payload_of


def calculate(application) -> list[ChargeCandidate]:
    data = payload_of(application)
    app_id = application.application_id
    item_qty = list_sum(data, ("items", "rows", "products", "sku_rows"), ("actual_qty", "qty", "quantity", "count"))
    box_qty = first_number(
        data,
        ("box_count", "boxes_count", "actual_boxes", "accepted_boxes", "places_count"),
        default="0",
    ) or list_count(data, ("boxes", "act_boxes"))
    pallet_qty = first_number(data, ("pallet_count", "pallets_count", "actual_pallets"), default="0") or list_count(
        data,
        ("pallets", "act_pallets", "flow_pallets"),
    )

    lines = [
        ChargeCandidate(
            service_code="receiving_goods",
            quantity=at_least_one(item_qty),
            operation_type="receiving",
            operation_id=app_id,
            source_key=f"receiving:{app_id}:goods",
            comment=f"Приёмка товара по заявке {app_id}",
        )
    ]
    if box_qty:
        lines.append(
            ChargeCandidate(
                service_code="receiving_unload_manual_1_15kg",
                quantity=box_qty,
                operation_type="receiving",
                operation_id=app_id,
                source_key=f"receiving:{app_id}:boxes",
                comment=f"Приёмка коробов по заявке {app_id}",
            )
        )
    if pallet_qty:
        lines.append(
            ChargeCandidate(
                service_code="receiving_pallet",
                quantity=pallet_qty,
                operation_type="receiving",
                operation_id=app_id,
                source_key=f"receiving:{app_id}:pallets",
                comment=f"Приёмка палет по заявке {app_id}",
            )
        )
    if data.get("placement") or data.get("placed") or data.get("warehouse_location"):
        lines.append(
            ChargeCandidate(
                service_code="receiving_distribute_destinations",
                quantity=Decimal("1"),
                operation_type="receiving",
                operation_id=app_id,
                source_key=f"receiving:{app_id}:placement",
                comment=f"Размещение по заявке {app_id}",
            )
        )
    return lines
