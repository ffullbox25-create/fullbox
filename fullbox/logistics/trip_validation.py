"""Проверки перед утверждением рейса (без параллельной системы)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone

from shipping.models import ShippingOrder

from .models import LogisticsTrip, LogisticsTripOrder, ShippingRoutingState


@dataclass
class TripValidationIssue:
    code: str
    message: str
    blocking: bool = True
    # HTML field/control to highlight in trip form (empty = general / order-level).
    field: str = ""


@dataclass
class TripValidationResult:
    issues: list[TripValidationIssue] = field(default_factory=list)
    totals: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(i.blocking for i in self.issues)

    @property
    def blocking_messages(self) -> list[str]:
        return [i.message for i in self.issues if i.blocking]

    @property
    def warning_messages(self) -> list[str]:
        return [i.message for i in self.issues if not i.blocking]

    @property
    def highlight_fields(self) -> list[str]:
        seen: list[str] = []
        for issue in self.issues:
            key = str(issue.field or "").strip()
            if key and key not in seen:
                seen.append(key)
        return seen

    def as_ui_payload(self) -> dict:
        return {
            "ok": self.ok,
            "blocking": [
                {"code": i.code, "message": i.message, "field": i.field}
                for i in self.issues
                if i.blocking
            ],
            "warnings": [
                {"code": i.code, "message": i.message, "field": i.field}
                for i in self.issues
                if not i.blocking
            ],
            "highlight_fields": self.highlight_fields,
            "totals": self.totals,
        }


def _dec(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _order_address(order: ShippingOrder) -> str:
    return str(
        order.destination_warehouse
        or order.destination_address
        or order.transit_address
        or ""
    ).strip()


def _is_client_pickup_trip(trip: LogisticsTrip) -> bool:
    if getattr(trip, "vehicle_type", "") == LogisticsTrip.VEHICLE_CLIENT:
        return True
    carrier = getattr(trip, "carrier", None)
    label = " ".join(
        str(value or "").strip()
        for value in (
            getattr(trip, "vehicle_name", ""),
            getattr(carrier, "name", ""),
            getattr(carrier, "short_name", ""),
        )
        if str(value or "").strip()
    ).lower().replace("ё", "е")
    return any(marker in label for marker in ("авто клиента", "транспорт клиента", "самовывоз", "client auto", "client car"))


def _order_requires_delivery_address(order: ShippingOrder, trip: LogisticsTrip) -> bool:
    no_address_delivery_types = {ShippingOrder.DELIVERY_PICKUP, ShippingOrder.DELIVERY_COURIER}
    return order.delivery_type not in no_address_delivery_types and not _is_client_pickup_trip(trip)


def _order_has_time_window(order: ShippingOrder) -> bool:
    return bool(order.slot_date or order.eta_at or order.planned_ship_date or order.slot_time)


def _trip_order_links(trip: LogisticsTrip) -> list[LogisticsTripOrder]:
    prefetched = getattr(trip, "_prefetched_objects_cache", {}).get("orders")
    if prefetched is not None:
        return list(prefetched)
    return list(
        trip.orders.select_related(
            "shipping_order",
            "shipping_order__agency",
            "shipping_order__routing_state",
        ).order_by("loading_sequence", "id")
    )


def collect_trip_cargo_totals(
    trip: LogisticsTrip,
    *,
    packing_payloads: dict[str, dict] | None = None,
    links: list[LogisticsTripOrder] | None = None,
) -> dict:
    packing_payloads = packing_payloads or {}
    links = list(links) if links is not None else _trip_order_links(trip)
    weight = Decimal("0")
    boxes = 0
    pallets = 0
    for link in links:
        order = link.shipping_order
        weight += _dec(getattr(order, "cargo_weight_kg", None))
        payload = packing_payloads.get(order.number) or {}
        boxes += int(
            payload.get("delivered_box_count")
            or getattr(order, "expected_boxes", None)
            or getattr(order, "cargo_package_count", None)
            or 0
        )
        pallets += int(payload.get("pallet_count") or 0)
        if not pallets and getattr(order, "place_type", None) == getattr(
            ShippingOrder, "PLACE_TYPE_PALLET", "pallet"
        ) and getattr(order, "cargo_package_count", None):
            pallets += int(order.cargo_package_count)
    return {
        "orders": len(links),
        "weight_kg": weight,
        "boxes": boxes,
        "pallets": pallets,
    }


def find_vehicle_conflicts(trip: LogisticsTrip) -> list[LogisticsTrip]:
    plate = str(trip.vehicle_number or "").strip()
    if not plate:
        return []
    qs = LogisticsTrip.objects.filter(
        vehicle_number__iexact=plate,
        status__in={
            LogisticsTrip.STATUS_PLANNED,
            LogisticsTrip.STATUS_LOADING,
            LogisticsTrip.STATUS_DEPARTED,
        },
    ).exclude(pk=trip.pk)
    if trip.trip_date:
        qs = qs.filter(Q(trip_date=trip.trip_date) | Q(trip_date__isnull=True))
    return list(qs[:5])


def find_driver_conflicts(trip: LogisticsTrip) -> list[LogisticsTrip]:
    driver = str(trip.driver_name or "").strip().lower()
    if not driver:
        return []
    qs = LogisticsTrip.objects.filter(
        status__in={
            LogisticsTrip.STATUS_PLANNED,
            LogisticsTrip.STATUS_LOADING,
            LogisticsTrip.STATUS_DEPARTED,
        },
    ).exclude(pk=trip.pk)
    if trip.trip_date:
        qs = qs.filter(Q(trip_date=trip.trip_date) | Q(trip_date__isnull=True))
    conflicts = []
    for other in qs.select_related()[:50]:
        if str(other.driver_name or "").strip().lower() == driver:
            conflicts.append(other)
        if len(conflicts) >= 5:
            break
    return conflicts


def validate_trip_for_finalize(
    trip: LogisticsTrip,
    *,
    packing_payloads: dict[str, dict] | None = None,
) -> TripValidationResult:
    """
    Полный набор проверок до утверждения рейса.
    Пример: «Невозможно утвердить рейс: вес груза превышает грузоподъёмность…»
    """
    result = TripValidationResult()
    links = _trip_order_links(trip)
    packing_payloads = packing_payloads or {}
    totals = collect_trip_cargo_totals(trip, packing_payloads=packing_payloads, links=links)
    result.totals = totals

    if not links:
        result.issues.append(
            TripValidationIssue("empty", "Невозможно утвердить рейс: нет заявок в составе.")
        )
        return result

    if not str(trip.driver_name or "").strip():
        result.issues.append(
            TripValidationIssue(
                "no_driver",
                "Невозможно утвердить рейс: не назначен водитель.",
                field="driver_name",
            )
        )
    if not str(trip.vehicle_number or "").strip():
        result.issues.append(
            TripValidationIssue(
                "no_vehicle",
                "Невозможно утвердить рейс: не назначен автомобиль (укажите госномер).",
                field="vehicle_number",
            )
        )
    if not trip.trip_date:
        result.issues.append(
            TripValidationIssue(
                "no_date",
                "Невозможно утвердить рейс: не указана дата рейса.",
                blocking=True,
                field="trip_date",
            )
        )
    if not getattr(trip, "carrier_id", None):
        result.issues.append(
            TripValidationIssue(
                "no_carrier",
                "Не выбран перевозчик — укажите перевозчика перед формированием рейса.",
                blocking=True,
                field="carrier_id",
            )
        )

    for link in links:
        order = link.shipping_order
        number = order.number or str(order.pk)
        if order.status == ShippingOrder.STATUS_CANCELED:
            result.issues.append(
                TripValidationIssue(
                    "canceled",
                    f"Невозможно утвердить рейс: заявка №{number} отменена.",
                )
            )
            continue
        if order.status != ShippingOrder.STATUS_PACKED:
            result.issues.append(
                TripValidationIssue(
                    "not_ready",
                    f"Невозможно утвердить рейс: заявка №{number} не готова (статус склада: {order.get_status_display()}).",
                    field="orders",
                )
            )
        try:
            routing = order.routing_state
        except ShippingRoutingState.DoesNotExist:
            routing = None
        if routing and routing.status == ShippingRoutingState.STATUS_CLARIFY:
            reason = routing.reason_label or routing.reason_code or "требуется уточнение"
            result.issues.append(
                TripValidationIssue(
                    "clarification",
                    f"Невозможно утвердить рейс: заявка №{number} возвращена на уточнение ({reason}).",
                )
            )
        if _order_requires_delivery_address(order, trip) and not _order_address(order):
            result.issues.append(
                TripValidationIssue(
                    "no_address",
                    f"Невозможно утвердить рейс: у заявки №{number} не заполнен адрес доставки.",
                    field="orders",
                )
            )
        if order.delivery_type == ShippingOrder.DELIVERY_MARKETPLACE and not _order_has_time_window(order):
            result.issues.append(
                TripValidationIssue(
                    "no_window",
                    f"Невозможно утвердить рейс: у заявки №{number} не заполнены дата/временное окно.",
                )
            )

    for other in find_vehicle_conflicts(trip):
        result.issues.append(
            TripValidationIssue(
                "vehicle_conflict",
                "Внимание: автомобиль уже назначен на рейс "
                f"{other.number} (статус: {other.get_status_display()}).",
                blocking=False,
                field="vehicle_number",
            )
        )
    for other in find_driver_conflicts(trip):
        result.issues.append(
            TripValidationIssue(
                "driver_conflict",
                "Внимание: водитель уже назначен на рейс "
                f"{other.number} (статус: {other.get_status_display()}).",
                blocking=False,
                field="driver_name",
            )
        )

    max_weight = trip.max_weight_kg
    if max_weight is not None and totals["weight_kg"] > _dec(max_weight):
        over = totals["weight_kg"] - _dec(max_weight)
        result.issues.append(
            TripValidationIssue(
                "weight",
                "Невозможно утвердить рейс: вес груза превышает грузоподъёмность автомобиля "
                f"на {over.quantize(Decimal('0.001'))} кг "
                f"(груз {totals['weight_kg']} кг, лимит {max_weight} кг).",
                field="max_weight_kg",
            )
        )
    elif max_weight is None and totals["weight_kg"] <= 0:
        result.issues.append(
            TripValidationIssue(
                "weight_unknown",
                "Вес заявок не заполнен — укажите грузоподъёмность автомобиля или вес в заявках.",
                blocking=False,
                field="max_weight_kg",
            )
        )

    if trip.max_pallets is not None and totals["pallets"] > int(trip.max_pallets):
        over = totals["pallets"] - int(trip.max_pallets)
        result.issues.append(
            TripValidationIssue(
                "pallets",
                f"Невозможно утвердить рейс: количество паллет превышает вместимость на {over} "
                f"(в рейсе {totals['pallets']}, лимит {trip.max_pallets}).",
                field="max_pallets",
            )
        )

    if trip.max_volume_m3 is not None:
        # Объём в заявках пока не хранится системно — лимит только предупреждение, если нет данных.
        result.issues.append(
            TripValidationIssue(
                "volume_limit_set",
                f"Задан лимит объёма {trip.max_volume_m3} м³. Проверьте загрузку вручную — "
                "автоматический расчёт м³ по заявкам пока недоступен.",
                blocking=False,
            )
        )

    return result


def raise_if_trip_invalid(trip: LogisticsTrip, *, packing_payloads: dict[str, dict] | None = None) -> TripValidationResult:
    result = validate_trip_for_finalize(trip, packing_payloads=packing_payloads)
    if not result.ok:
        raise ValueError(result.blocking_messages[0])
    return result
