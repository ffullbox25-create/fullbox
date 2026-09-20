from __future__ import annotations

from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.models import OrderAuditEntry, log_order_action
from employees.models import Employee
from logistics.models import LogisticsTrip, LogisticsTripOrder, display_trip_number

from .item_binding import shipping_final_truth_rows
from .models import ShippingOrder
from .services import (
    close_logistician_task,
    close_shipping_act_logistician_task,
    close_shipping_act_manager_task,
    close_storekeeper_task,
    ensure_shipping_act_logistician_task,
    ensure_shipping_act_manager_task,
)


DISPATCH_ACT_DELIVERY_TYPES = {
    ShippingOrder.DELIVERY_MARKETPLACE,
    ShippingOrder.DELIVERY_PICKUP,
    ShippingOrder.DELIVERY_COURIER,
}


def shipping_dispatch_trip_link(order: ShippingOrder) -> LogisticsTripOrder | None:
    return (
        LogisticsTripOrder.objects.select_related("trip", "shipping_order")
        .filter(
            shipping_order=order,
            trip__status__in=[
                LogisticsTrip.STATUS_DRAFT,
                LogisticsTrip.STATUS_PLANNED,
                LogisticsTrip.STATUS_LOADING,
                LogisticsTrip.STATUS_DEPARTED,
                LogisticsTrip.STATUS_COMPLETED,
            ],
        )
        .order_by("-trip__created_at", "-id")
        .first()
    )


def shipping_dispatch_act_entry(order: ShippingOrder) -> OrderAuditEntry | None:
    return (
        OrderAuditEntry.objects.filter(
            order_id=order.number,
            order_type="shipping",
            payload__act="shipping_dispatch_act",
        )
        .order_by("-created_at")
        .first()
    )


def _trip_is_public(trip: LogisticsTrip | None) -> bool:
    return bool(
        trip
        and trip.status
        in {
            LogisticsTrip.STATUS_LOADING,
            LogisticsTrip.STATUS_DEPARTED,
            LogisticsTrip.STATUS_COMPLETED,
            LogisticsTrip.STATUS_CANCELED,
        }
    )


def shipping_dispatch_employee(payload: dict | None, key: str) -> Employee | None:
    raw_value = (payload or {}).get(key)
    try:
        employee_id = int(raw_value)
    except (TypeError, ValueError):
        return None
    return Employee.objects.filter(id=employee_id, is_active=True).first()


def format_shipping_datetime(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return timezone.localtime(parsed).strftime("%d.%m.%Y %H:%M")


def shipping_dispatch_payload(
    order: ShippingOrder,
    *,
    packing_summary: dict | None = None,
    payload: dict | None = None,
) -> dict:
    existing = dict(payload or {})
    trip_link = shipping_dispatch_trip_link(order)
    trip = trip_link.trip if trip_link else None
    public_trip = trip if _trip_is_public(trip) else None
    packing_summary = packing_summary or {}

    items = shipping_final_truth_rows(order)
    if not items:
        items = []
        for item in order.items.order_by("id"):
            items.append(
                {
                    "sku_code": item.sku_code,
                    "name": item.name,
                    "size": item.size,
                    "barcode": item.barcode,
                    "qty_requested": int(item.qty_requested or 0),
                    "qty_reserved": int(item.qty_reserved or 0),
                    "qty_shipped": int(item.qty_shipped or 0),
                    "shortage_qty": max(
                        int(item.qty_requested or 0) - int(item.qty_shipped or 0),
                        0,
                    ),
                    "excess_qty": 0,
                }
            )

    payload_base = {
        "act": "shipping_dispatch_act",
        "act_label": "Акт отгрузки",
        "trip_id": public_trip.id if public_trip else None,
        "trip_number": display_trip_number(public_trip.number) if public_trip else "",
        "trip_status": public_trip.status if public_trip else "",
        "trip_date": public_trip.trip_date.isoformat() if public_trip and public_trip.trip_date else "",
        "trip_vehicle_type": public_trip.vehicle_type if public_trip else "",
        "trip_vehicle_name": public_trip.vehicle_name if public_trip else "",
        "trip_vehicle_number": public_trip.vehicle_number if public_trip else "",
        "trip_driver_name": public_trip.driver_name if public_trip else "",
        "trip_driver_phone": public_trip.driver_phone if public_trip else "",
        "trip_route_comment": public_trip.route_comment if public_trip else "",
        "trip_loading_comment": public_trip.loading_comment if public_trip else "",
        "loading_sequence": trip_link.loading_sequence if public_trip and trip_link else 0,
        "delivery_sequence": trip_link.delivery_sequence if public_trip and trip_link else 0,
        "trip_order_comment": trip_link.comment if public_trip and trip_link else "",
        "vehicle_type": order.vehicle_type or (public_trip.vehicle_type if public_trip else ""),
        "vehicle_type_label": order.get_vehicle_type_display()
        if order.vehicle_type
        else (public_trip.get_vehicle_type_display() if public_trip else "-"),
        "vehicle_number": order.vehicle_number or (public_trip.vehicle_number if public_trip else ""),
        "driver_phone": order.driver_phone or (public_trip.driver_phone if public_trip else ""),
        "driver_name": public_trip.driver_name if public_trip else "",
        "destination_warehouse": order.destination_warehouse,
        "destination_address": order.destination_address,
        "slot_date": order.slot_date.isoformat() if order.slot_date else "",
        "slot_time": order.slot_time.strftime("%H:%M") if order.slot_time else "",
        "eta_at": order.eta_at.isoformat() if order.eta_at else "",
        "planned_ship_date": order.planned_ship_date.isoformat() if order.planned_ship_date else "",
        "pallet_count": int(packing_summary.get("pallet_count") or 0),
        "box_count": int(packing_summary.get("box_count") or 0),
        "act_pallets": [
            {
                "label": pallet.get("label") or "-",
                "code": pallet.get("code") or "",
                "box_count": int(pallet.get("box_count") or 0),
                "qty": int(pallet.get("qty") or 0),
            }
            for pallet in (packing_summary.get("pallets") or [])
        ],
        "act_items": items,
    }
    payload_base.update(existing)
    # The document composition is always rebuilt from the immutable final truth.
    payload_base["act_items"] = items
    return payload_base


def shipping_dispatch_stage(order: ShippingOrder, *, packing_summary: dict | None = None) -> dict:
    act_entry = shipping_dispatch_act_entry(order)
    raw_payload = dict(act_entry.payload or {}) if act_entry and isinstance(act_entry.payload, dict) else {}
    trip_link = shipping_dispatch_trip_link(order)
    trip = trip_link.trip if trip_link else None
    vehicle_type = str(raw_payload.get("vehicle_type") or order.vehicle_type or "").strip()
    requires_trip = vehicle_type == ShippingOrder.VEHICLE_FULFILLMENT
    has_trip = _trip_is_public(trip)
    logistician_signed = bool(raw_payload.get("act_logistician_signed"))
    manager_signed = bool(raw_payload.get("act_manager_signed"))
    act_sent = bool(raw_payload.get("act_sent"))
    warehouse_confirmed = bool(raw_payload.get("act_warehouse_confirmed"))
    return {
        "entry": act_entry,
        "payload": shipping_dispatch_payload(
            order,
            packing_summary=packing_summary,
            payload=raw_payload,
        ),
        "trip_link": trip_link if has_trip else None,
        "requires_trip": requires_trip,
        "has_trip": has_trip,
        "logistician_signed": logistician_signed,
        "manager_signed": manager_signed,
        "act_sent": act_sent,
        "warehouse_confirmed": warehouse_confirmed,
        "warehouse_confirmed_at_label": format_shipping_datetime(
            raw_payload.get("act_warehouse_confirmed_at")
        ),
        "logistician_signed_at_label": format_shipping_datetime(raw_payload.get("act_logistician_signed_at")),
        "manager_signed_at_label": format_shipping_datetime(raw_payload.get("act_manager_signed_at")),
        "act_sent_at_label": format_shipping_datetime(raw_payload.get("act_sent_at")),
    }


@transaction.atomic
def confirm_dispatch_act_from_loading(
    order: ShippingOrder,
    user,
    *,
    packing_summary: dict | None = None,
) -> bool:
    """Create the mandatory act only from a successfully persisted loading fact."""
    if order.delivery_type not in DISPATCH_ACT_DELIVERY_TYPES:
        return False
    if not order.is_closed():
        raise ValidationError(
            "Акт отгрузки создается только после подтвержденных сканов и складского списания."
        )

    stage = shipping_dispatch_stage(order, packing_summary=packing_summary)
    payload = dict(stage["payload"] or {})
    if stage["warehouse_confirmed"]:
        if not stage["logistician_signed"]:
            ensure_shipping_act_logistician_task(order, user)
        return False

    confirmed_at = timezone.localtime().isoformat()
    payload["act_warehouse_confirmed"] = True
    payload["act_warehouse_confirmed_at"] = confirmed_at
    payload["act_state"] = "warehouse_confirmed"
    log_order_action(
        "status",
        order_id=order.number,
        order_type="shipping",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=order.agency,
        description="Акт отгрузки сформирован по подтвержденным сканам погрузки",
        payload=shipping_dispatch_payload(
            order,
            packing_summary=packing_summary,
            payload=payload,
        ),
    )
    ensure_shipping_act_logistician_task(order, user)
    return True


def _fallback_logistician_employee(user) -> Employee | None:
    return (
        Employee.objects.filter(user=user, role="logistician", is_active=True).first()
        or Employee.objects.filter(role="logistician", is_active=True).order_by("full_name").first()
    )


def _fallback_manager_employee(user) -> Employee | None:
    return (
        Employee.objects.filter(user=user, is_active=True).first()
        or Employee.objects.filter(role__in=["manager", "head_manager"], is_active=True).order_by("full_name").first()
    )


def sign_dispatch_act_logistician(
    order: ShippingOrder,
    user,
    *,
    dispatch_stage: dict | None = None,
    packing_summary: dict | None = None,
    employee: Employee | None = None,
) -> None:
    dispatch_stage = dispatch_stage or shipping_dispatch_stage(order, packing_summary=packing_summary)
    if not dispatch_stage["warehouse_confirmed"]:
        raise ValidationError(
            "Акт нельзя подписать до подтвержденных сканов и завершения складской погрузки."
        )
    payload = dict(dispatch_stage["payload"] or {})
    employee = employee or _fallback_logistician_employee(user)
    payload["act_logistician_signed"] = True
    payload["act_logistician_signed_at"] = timezone.localtime().isoformat()
    payload["act_logistician_employee_id"] = employee.id if employee else None
    payload["act_state"] = "logistician_signed"
    log_order_action(
        "status",
        order_id=order.number,
        order_type="shipping",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=order.agency,
        description="Акт отгрузки подписан логистом",
        payload=shipping_dispatch_payload(
            order,
            packing_summary=packing_summary,
            payload=payload,
        ),
    )
    close_shipping_act_logistician_task(order)
    ensure_shipping_act_manager_task(order, user, observer=employee)


def sign_dispatch_act_manager(
    order: ShippingOrder,
    user,
    *,
    dispatch_stage: dict | None = None,
    packing_summary: dict | None = None,
    employee: Employee | None = None,
) -> None:
    dispatch_stage = dispatch_stage or shipping_dispatch_stage(order, packing_summary=packing_summary)
    if not dispatch_stage["warehouse_confirmed"]:
        raise ValidationError(
            "Акт нельзя подписать до подтвержденных сканов и завершения складской погрузки."
        )
    if not dispatch_stage["logistician_signed"]:
        raise ValidationError("Сначала акт должен подписать логист.")
    if not order.is_closed():
        raise ValidationError(
            "Акт нельзя отправить клиенту: складское списание по сканам еще не завершено."
        )
    payload = dict(dispatch_stage["payload"] or {})
    employee = employee or _fallback_manager_employee(user)
    payload["act_manager_signed"] = True
    payload["act_manager_signed_at"] = timezone.localtime().isoformat()
    payload["act_manager_employee_id"] = employee.id if employee else None
    payload["act_sent"] = True
    payload["act_sent_at"] = timezone.localtime().isoformat()
    payload["act_state"] = "sent"
    with transaction.atomic():
        log_order_action(
            "status",
            order_id=order.number,
            order_type="shipping",
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=order.agency,
            description="Акт отгрузки подписан менеджером, отправлен клиенту. Заявка закрыта",
            payload=shipping_dispatch_payload(
                order,
                packing_summary=packing_summary,
                payload=payload,
            ),
        )
        close_storekeeper_task(order)
        close_logistician_task(order)
        close_shipping_act_logistician_task(order)
        close_shipping_act_manager_task(order)
