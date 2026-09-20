from __future__ import annotations

from decimal import Decimal

from .base import ChargeCandidate
from .utils import at_least_one, first_number, list_count, list_sum, payload_of


def _shipping_order(application):
    try:
        from shipping.models import ShippingOrder
    except Exception:
        return None
    qs = ShippingOrder.objects.filter(number=application.application_id)
    if application.client_id:
        qs = qs.filter(agency_id=application.client_id)
    return qs.select_related("marketplace").prefetch_related("items").order_by("-id").first()


def calculate(application) -> list[ChargeCandidate]:
    data = payload_of(application)
    app_id = application.application_id
    order = _shipping_order(application)

    item_qty = Decimal("0")
    pallet_qty = Decimal("0")
    box_qty = Decimal("0")
    delivery_qty = Decimal("0")

    if order is not None:
        for item in order.items.all():
            item_qty += Decimal(getattr(item, "qty_shipped", 0) or getattr(item, "qty_reserved", 0) or getattr(item, "qty_requested", 0) or 0)
        expected_boxes = getattr(order, "expected_boxes", 0) or 0
        place_type = str(getattr(order, "place_type", "") or "").lower()
        supply_type = str(getattr(order, "supply_type", "") or "").lower()
        actual_pallets = 0
        try:
            from shipping.packing import _shipping_packing_summary

            actual_pallets = int((_shipping_packing_summary(order) or {}).get("pallet_count") or 0)
        except Exception:
            actual_pallets = 0
        if place_type == "pallet" or supply_type in {"monopallet", "supersafe"}:
            # Фактическая паллетизация склада имеет приоритет; план используется только до неё.
            pallet_qty = Decimal(actual_pallets or expected_boxes or 1)
        elif expected_boxes:
            box_qty = Decimal(expected_boxes)
        if str(getattr(order, "vehicle_type", "") or "") == getattr(order, "VEHICLE_FULFILLMENT", "fulfillment"):
            delivery_qty = pallet_qty or Decimal("1")

    item_qty = item_qty or list_sum(data, ("items", "rows", "products", "sku_rows"), ("qty_shipped", "qty_reserved", "qty", "quantity", "count"))
    pallet_qty = pallet_qty or first_number(data, ("pallet_count", "pallets_count", "expected_pallets"), default="0") or list_count(
        data,
        ("pallets", "flow_pallets", "shipping_pallets"),
    )

    lines = [
        ChargeCandidate(
            service_code="shipping_pick_storage_item",
            quantity=at_least_one(item_qty),
            operation_type="shipping",
            operation_id=app_id,
            source_key=f"shipping:{app_id}:pick_items",
            comment=f"Подбор товаров по отгрузке {app_id}",
        ),
        ChargeCandidate(
            service_code="shipping_load_boxes_upto_25kg",
            quantity=Decimal("1"),
            operation_type="shipping",
            operation_id=app_id,
            source_key=f"shipping:{app_id}:dispatch",
            comment=f"Отгрузка по заявке {app_id}",
        ),
    ]
    if pallet_qty:
        lines.extend(
            [
                ChargeCandidate(
                    service_code="shipping_form_pallet",
                    quantity=pallet_qty,
                    operation_type="shipping",
                    operation_id=app_id,
                    source_key=f"shipping:{app_id}:form_pallet",
                    comment=f"Формирование палет по отгрузке {app_id}",
                ),
                ChargeCandidate(
                    service_code="shipping_load_pallet_upto_500kg",
                    quantity=pallet_qty,
                    operation_type="shipping",
                    operation_id=app_id,
                    source_key=f"shipping:{app_id}:release_pallet",
                    comment=f"Выдача палет по отгрузке {app_id}",
                ),
            ]
        )
    if delivery_qty:
        lines.append(
            ChargeCandidate(
                service_code="logistics_pickup_goods_market",
                quantity=delivery_qty,
                operation_type="shipping",
                operation_id=app_id,
                source_key=f"shipping:{app_id}:delivery_pallet",
                comment=f"Доставка палет по отгрузке {app_id}",
            )
        )
    if box_qty and not pallet_qty:
        lines.append(
            ChargeCandidate(
                service_code="extra_warehouse_operation",
                quantity=box_qty,
                operation_type="shipping",
                operation_id=app_id,
                source_key=f"shipping:{app_id}:places",
                comment=f"Места отгрузки по заявке {app_id}",
            )
        )
    return lines
