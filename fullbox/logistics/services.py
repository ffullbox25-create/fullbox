from __future__ import annotations

import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import OuterRef, Prefetch, Subquery
from django.db.models.functions import Coalesce
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone

from agent.models import DeviceAgent
from audit.models import OrderAuditEntry
from employees.access import get_employee_for_user, resolve_cabinet_url
from employees.models import Employee
from fullbox.order_numbers import format_order_number
from head_manager.models import Carrier, OwnCompany
from shipping.models import ShippingOrder
from shipping.packing import _shipping_packing_summary, shipping_packing_slips_data
from sklad.models import WarehouseReserve, WarehouseStockSnapshot
from sklad.services import WarehouseGoodsStateResolver, WarehouseMovementResolver
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from todo.models import Task

from .models import (
    CarrierVehicle,
    LogisticsTrip,
    LogisticsTripOrder,
    ShippingRoutingState,
    display_trip_number,
    is_draft_trip_number,
    next_draft_trip_number,
    next_trip_number,
    trip_number_sequence_value,
)


ALLOWED_ROLES = ("logistician", "manager", "head_manager", "director", "admin", "developer")


def _shell_context(role: str | None, *, trips_subnav: str) -> dict:
    """Флаги единой оболочки кабинета для страниц /logistics/."""
    from teammanager.roles import CABINET_ROLES

    return {
        "active_nav": "trips",
        "trips_subnav": trips_subnav,
        "use_cabinet_shell": (role or "") in CABINET_ROLES,
    }

TRIP_READ_ROLES = ALLOWED_ROLES + ("storekeeper",)
logger = logging.getLogger(__name__)
PREPARING_STATUSES = [
    ShippingOrder.STATUS_SUBMITTED,
    ShippingOrder.STATUS_RESERVED,
    ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
    ShippingOrder.STATUS_PICKING,
]
READY_STATUSES = [ShippingOrder.STATUS_PACKED]
TRIP_PREPLANNING_STATUSES = [
    ShippingOrder.STATUS_RESERVED,
    ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
    ShippingOrder.STATUS_PICKING,
    ShippingOrder.STATUS_PACKED,
]
TRIP_EXCLUDED_DELIVERY_TYPES = (
    ShippingOrder.DELIVERY_TRANSFER,
)
ACTIVE_TRIP_STATUSES = [
    LogisticsTrip.STATUS_DRAFT,
    LogisticsTrip.STATUS_PLANNED,
    LogisticsTrip.STATUS_LOADING,
    LogisticsTrip.STATUS_DEPARTED,
]
SHIPPING_STATUS_LABELS = {
    ShippingOrder.STATUS_DRAFT: "Черновик клиента",
    ShippingOrder.STATUS_SUBMITTED: "На согласовании менеджера",
    ShippingOrder.STATUS_RESERVED: "Согласована и передана в работу кладовщику",
    ShippingOrder.STATUS_STOREKEEPER_ACCEPTED: "Принята в работу складом",
    ShippingOrder.STATUS_PICKING: "Доставка в OTG",
    ShippingOrder.STATUS_PACKED: "Подготовлена складом",
    ShippingOrder.STATUS_SHIPPED: "Отгружена",
    ShippingOrder.STATUS_PARTIAL: "Отгружена частично",
    ShippingOrder.STATUS_CANCELED: "Отменена",
}
TRIP_PREPLANNING_STATUS_LABELS = {
    ShippingOrder.STATUS_SUBMITTED: "Ожидает менеджера",
    ShippingOrder.STATUS_RESERVED: "Ожидает склад",
    ShippingOrder.STATUS_STOREKEEPER_ACCEPTED: "Склад принял",
    ShippingOrder.STATUS_PICKING: "В отборе",
    ShippingOrder.STATUS_PACKED: "Сборка подтверждена",
}
TRIP_STATUS_LABELS = dict(LogisticsTrip.STATUS_CHOICES)
PLATE_LETTER_MAP = str.maketrans(
    {
        "A": "А",
        "B": "В",
        "E": "Е",
        "K": "К",
        "M": "М",
        "H": "Н",
        "O": "О",
        "P": "Р",
        "C": "С",
        "T": "Т",
        "Y": "У",
        "X": "Х",
    }
)
PLATE_REGEX = re.compile(r"^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}$")
TRIP_LOADING_AUDIT_TYPE = "logistics_trip"
TRIP_LOADING_AUDIT_ACT = "trip_loading_progress"


def _display_shipping_status(
    order: ShippingOrder,
    *,
    is_in_trip: bool = False,
    is_loaded_for_trip: bool = False,
    movement=None,
) -> str:
    trip_status = ""
    if is_loaded_for_trip:
        trip_status = LogisticsTrip.STATUS_DEPARTED
    elif is_in_trip and order.status == ShippingOrder.STATUS_PACKED:
        trip_status = LogisticsTrip.STATUS_PLANNED
    result = WarehouseGoodsStateResolver.resolve_for_shipping_order(
        order,
        trip_status=trip_status,
        movement=movement,
    )
    return result.label_for("logistician")


def _short_agency_name(name: str | None) -> str:
    raw = str(name or "").strip()
    if not raw:
        return "-"
    return re.sub(r"^Индивидуальный предприниматель\b", "ИП", raw, flags=re.IGNORECASE)


def _parse_date(value: str | None):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _normalize_vehicle_number(value: str | None) -> str:
    raw = re.sub(r"[^0-9A-Za-zА-Яа-я]", "", str(value or "").upper()).translate(PLATE_LETTER_MAP)
    if not raw:
        return ""
    if not PLATE_REGEX.match(raw):
        raise ValueError("Укажите номер машины в формате А123ВС77 или А123ВС777.")
    return raw


def _format_vehicle_number_for_plate(value: str | None) -> str:
    raw = str(value or "").strip()
    if len(raw) < 6:
        return raw
    return f"{raw[0]} {raw[1:4]} {raw[4:6]} {raw[6:]}"


def _default_own_company() -> OwnCompany | None:
    return OwnCompany.objects.filter(is_active=True).order_by("-is_default", "name").first()


def _trip_shipper_name() -> str:
    company = _default_own_company()
    if company is None:
        return 'ООО "ФуллБокс"'
    return str(company.short_name or company.name or 'ООО "ФуллБокс"').strip()


def _trip_customer_summary(trip_orders: list[LogisticsTripOrder]) -> str:
    values: list[str] = []
    for item in trip_orders:
        agency = getattr(item.shipping_order, "agency", None)
        candidate = str(getattr(agency, "short_name", "") or getattr(agency, "agn_name", "") or "").strip()
        if candidate:
            values.append(candidate)
    unique = list(dict.fromkeys(values))
    return ", ".join(unique) if unique else "-"


def _order_route_destination(order: ShippingOrder) -> str:
    return str(
        order.destination_warehouse
        or order.destination_address
        or order.transit_address
        or ""
    ).strip()


def _trip_consignee_summary(trip_orders: list[LogisticsTripOrder]) -> str:
    values: list[str] = []
    for item in trip_orders:
        order = item.shipping_order
        candidate = _order_route_destination(order)
        if candidate:
            values.append(candidate)
    unique = list(dict.fromkeys(values))
    return ", ".join(unique) if unique else "-"


def _carrier_queryset():
    return Carrier.objects.filter(is_active=True).order_by("short_name", "name", "id")


def _carrier_options_queryset(*, exclude_trip_id: int | None = None):
    vehicle_defaults = CarrierVehicle.objects.filter(
        carrier_id=OuterRef("pk"),
        is_active=True,
    ).order_by("-is_default", "-updated_at", "-id")
    latest_transport = (
        LogisticsTrip.objects.filter(carrier_id=OuterRef("pk"))
        .exclude(status=LogisticsTrip.STATUS_CANCELED)
        .exclude(vehicle_number="")
        .exclude(driver_name="")
        .exclude(driver_phone="")
    )
    if exclude_trip_id:
        latest_transport = latest_transport.exclude(pk=exclude_trip_id)
    latest_transport = latest_transport.order_by("-updated_at", "-id")
    return _carrier_queryset().annotate(
        default_vehicle_number=Coalesce(
            Subquery(vehicle_defaults.values("vehicle_number")[:1]),
            Subquery(latest_transport.values("vehicle_number")[:1]),
        ),
        default_driver_name=Coalesce(
            Subquery(vehicle_defaults.values("driver_name")[:1]),
            Subquery(latest_transport.values("driver_name")[:1]),
        ),
        default_driver_phone=Coalesce(
            Subquery(vehicle_defaults.values("driver_phone")[:1]),
            Subquery(latest_transport.values("driver_phone")[:1]),
        ),
        default_max_weight_kg=Coalesce(
            Subquery(vehicle_defaults.values("max_weight_kg")[:1]),
            Subquery(latest_transport.values("max_weight_kg")[:1]),
        ),
        default_max_volume_m3=Coalesce(
            Subquery(vehicle_defaults.values("max_volume_m3")[:1]),
            Subquery(latest_transport.values("max_volume_m3")[:1]),
        ),
        default_max_pallets=Coalesce(
            Subquery(vehicle_defaults.values("max_pallets")[:1]),
            Subquery(latest_transport.values("max_pallets")[:1]),
        ),
    )


def _resolve_trip_carrier(request) -> Carrier | None:
    carrier_id = _parse_int(request.POST.get("carrier_id"))
    if carrier_id <= 0:
        return None
    carrier = _carrier_queryset().filter(pk=carrier_id).first()
    if carrier is None:
        raise ValueError("Выберите перевозчика из справочника.")
    return carrier


def _normalize_driver_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("Телефон водителя укажите в формате +7 900 000-00-00.")
    return f"+7 {digits[0:3]} {digits[3:6]}-{digits[6:8]}-{digits[8:10]}"


def _first_active_employee_by_roles(*roles: str) -> Employee | None:
    for role in [role for role in roles if role]:
        employee = Employee.objects.filter(role=role, is_active=True).order_by("full_name").first()
        if employee:
            return employee
    return None


def _trip_is_draft(trip: LogisticsTrip) -> bool:
    return trip.status in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED} and is_draft_trip_number(trip.number)


def _trip_public_number(trip: LogisticsTrip) -> str:
    if _trip_is_draft(trip):
        return "Черновик"
    return display_trip_number(trip.number)


def _trip_status_label(trip: LogisticsTrip) -> str:
    if _trip_is_draft(trip):
        return "Черновик"
    return TRIP_STATUS_LABELS.get(trip.status, trip.get_status_display())


def _assign_public_trip_number(trip: LogisticsTrip) -> str:
    sequence = trip_number_sequence_value(trip.number)
    if sequence > 0:
        candidate = f"{sequence}_RS"
        conflict = LogisticsTrip.objects.exclude(pk=trip.pk).filter(number=candidate).exists()
        if not conflict:
            return candidate
    return next_trip_number()


def _can_manage_trip(role: str | None) -> bool:
    return role in ALLOWED_ROLES


def _can_edit_trip(role: str | None, trip: LogisticsTrip) -> bool:
    return _can_manage_trip(role) and trip.status in {
        LogisticsTrip.STATUS_DRAFT,
        LogisticsTrip.STATUS_PLANNED,
        LogisticsTrip.STATUS_LOADING,
    }


def _can_fill_missing_trip_transport(role: str | None, trip: LogisticsTrip) -> bool:
    return (
        _can_manage_trip(role)
        and trip.status == LogisticsTrip.STATUS_LOADING
        and not all((trip.vehicle_number.strip(), trip.driver_name.strip(), trip.driver_phone.strip()))
    )


def _can_add_orders_to_trip(role: str | None, trip: LogisticsTrip) -> bool:
    """Allow topping up a trip until the vehicle has actually departed."""
    return _can_manage_trip(role) and trip.status in {
        LogisticsTrip.STATUS_DRAFT,
        LogisticsTrip.STATUS_PLANNED,
        LogisticsTrip.STATUS_LOADING,
    }


def _shipping_order_can_be_removed_from_trip(order: ShippingOrder | None) -> bool:
    if order is None:
        return False
    return order.status not in {
        ShippingOrder.STATUS_SHIPPED,
        ShippingOrder.STATUS_PARTIAL,
        ShippingOrder.STATUS_CANCELED,
    }


def _can_remove_orders_from_trip(role: str | None, trip: LogisticsTrip) -> bool:
    """Allow removing an order from a formed trip until shipping actually happened."""
    return _can_manage_trip(role) and trip.status in {
        LogisticsTrip.STATUS_DRAFT,
        LogisticsTrip.STATUS_PLANNED,
        LogisticsTrip.STATUS_LOADING,
    }


def _can_return_trip(role: str | None, trip: LogisticsTrip) -> bool:
    return role == "storekeeper" and trip.status == LogisticsTrip.STATUS_LOADING


def _trip_route(trip: LogisticsTrip) -> str:
    return f"/logistics/trips/{trip.pk}/"


def _trip_title(trip: LogisticsTrip) -> str:
    if _trip_is_draft(trip):
        return "Рейс · Черновик"
    return f"Рейс №{_trip_public_number(trip)}"


def _trip_loading_url(trip: LogisticsTrip) -> str:
    return f"/logistics/trips/{trip.pk}/loading/"


def _can_start_loading(role: str | None, trip: LogisticsTrip) -> bool:
    return role == "storekeeper" and trip.status == LogisticsTrip.STATUS_LOADING


def _can_begin_loading(role: str | None, trip: LogisticsTrip) -> bool:
    return role == "storekeeper" and trip.status == LogisticsTrip.STATUS_LOADING


def _can_mark_mp_delivery(role: str | None, trip: LogisticsTrip) -> bool:
    """Logistician/manager marks marketplace handoff after departure."""
    return _can_manage_trip(role) and trip.status == LogisticsTrip.STATUS_DEPARTED


def _ship_single_trip_order(order, *, user=None) -> None:
    from shipping.services import ship_order as finalize_shipping_order

    if order is None or not order.items.exists() or order.is_closed():
        return
    finalize_shipping_order(order, user)


def _scanner_agents_payload() -> list[dict]:
    try:
        online_threshold = timezone.now() - timedelta(seconds=30)
        payload: list[dict] = []
        for agent in DeviceAgent.objects.all().order_by("-last_seen", "-updated_at"):
            is_online = bool(agent.last_seen and agent.last_seen >= online_threshold)
            meta = agent.meta if isinstance(agent.meta, dict) else {}
            com_status = meta.get("com_status") if isinstance(meta.get("com_status"), dict) else {}
            com_config = meta.get("com") if isinstance(meta.get("com"), dict) else {}
            payload.append(
                {
                    "agent_id": agent.agent_id,
                    "title": agent.name or agent.host or agent.agent_id,
                    "status": "онлайн" if is_online else "нет связи",
                    "is_online": is_online,
                    "host": agent.host,
                    "version": agent.version,
                    "last_seen": agent.last_seen.isoformat() if agent.last_seen else "",
                    "com_port": str(
                        com_status.get("port") or com_config.get("port") or com_config.get("port_name") or ""
                    ).strip(),
                    "com_enabled": bool(
                        com_status.get("enabled") if "enabled" in com_status else com_config.get("enabled")
                    ),
                    "com_connected": bool(com_status.get("connected")),
                    "com_error": str(com_status.get("error") or "").strip(),
                }
            )
        return payload
    except DatabaseError:
        return []


def _normalize_loading_scan(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).upper()


def _trip_loading_progress_key(order_id: int, pallet_key: str | None) -> str:
    normalized_key = _normalize_loading_scan(pallet_key)
    if not normalized_key:
        return ""
    return f"{int(order_id)}::{normalized_key}"


def _trip_loading_entry(trip: LogisticsTrip) -> OrderAuditEntry | None:
    return (
        OrderAuditEntry.objects.filter(
            order_id=str(trip.pk),
            order_type=TRIP_LOADING_AUDIT_TYPE,
            payload__act=TRIP_LOADING_AUDIT_ACT,
        )
        .order_by("-created_at")
        .first()
    )


def _trip_loading_payload(trip: LogisticsTrip) -> dict:
    entry = _trip_loading_entry(trip)
    if not entry or not isinstance(entry.payload, dict):
        return {}
    return dict(entry.payload or {})


def _shipping_trip_pallet_error(order: ShippingOrder, packing_summary: dict | None = None) -> str:
    summary = packing_summary if isinstance(packing_summary, dict) else (_shipping_packing_summary(order) or {})
    pallets = [
        pallet
        for pallet in (summary.get("pallets") or summary.get("act_pallets") or [])
        if isinstance(pallet, dict)
    ]
    pallet_codes = {
        str(pallet.get("code") or "").strip().lower()
        for pallet in pallets
        if str(pallet.get("code") or "").strip()
    }
    if not pallets or not pallet_codes:
        return "у заявки ноль паллет"
    try:
        cached_snapshots = getattr(order, "_shipping_flow_snapshots_cache", None)
        snapshots_for_validation = (
            cached_snapshots
            if getattr(order, "_shipping_flow_snapshots_cache_complete", False)
            else None
        )
        ownership = WarehouseWritePathService.validate_shipping_pallet_ownership(
            agency=order.agency,
            order_id=order.number,
            snapshots=snapshots_for_validation,
            require_pallets=True,
        )
    except ValueError as exc:
        return str(exc)
    warehouse_codes = {
        str(code or "").strip().lower()
        for code in ownership.get("pallet_codes") or []
        if str(code or "").strip()
    }
    if pallet_codes != warehouse_codes:
        return "состав паллет в акте не совпадает со складскими паллетами заявки"
    return ""


def _shipping_packing_item_count(packing_summary: dict | None) -> int:
    summary = packing_summary if isinstance(packing_summary, dict) else {}
    act_item_count = sum(
        int(_parse_int(line.get("qty")))
        for line in (summary.get("act_items") or [])
        if isinstance(line, dict)
    )
    if act_item_count > 0:
        return act_item_count
    box_item_count = sum(
        int(_parse_int(box.get("qty")))
        for pallet in (summary.get("pallets") or [])
        if isinstance(pallet, dict)
        for box in (pallet.get("boxes") or [])
        if isinstance(box, dict)
    )
    if box_item_count > 0:
        return box_item_count
    return sum(
        int(_parse_int(pallet.get("qty")))
        for pallet in (summary.get("pallets") or [])
        if isinstance(pallet, dict)
    )


def _trip_loading_rows(trip: LogisticsTrip) -> list[dict]:
    prefetched = getattr(trip, "_prefetched_objects_cache", {}).get("orders")
    if prefetched is None:
        trip_orders = list(
            trip.orders.select_related("shipping_order", "shipping_order__agency")
            .order_by("-delivery_sequence", "-loading_sequence", "-id")
        )
    else:
        trip_orders = sorted(
            prefetched,
            key=lambda item: (
                int(item.delivery_sequence or 0),
                int(item.loading_sequence or 0),
                int(item.id or 0),
            ),
            reverse=True,
        )
    rows: list[dict] = []
    for position, item in enumerate(trip_orders, start=1):
        order = item.shipping_order
        packing_summary = _shipping_packing_summary(order) or {}
        blocking_error = _shipping_trip_pallet_error(order, packing_summary)
        pallets_raw = shipping_packing_slips_data(order, packing_summary) if packing_summary else []
        pallets: list[dict] = []
        for pallet in pallets_raw:
            load_key = str(pallet.get("pallet_code") or pallet.get("qr_value") or pallet.get("slip_key") or "").strip()
            accepted_scans = {
                _normalize_loading_scan(load_key),
                _normalize_loading_scan(pallet.get("pallet_code")),
                _normalize_loading_scan(pallet.get("qr_value")),
            }
            accepted_scans.discard("")
            pallets.append(
                {
                    "load_key": load_key,
                    "load_key_norm": _normalize_loading_scan(load_key),
                    "progress_key": _trip_loading_progress_key(order.pk, load_key),
                    "pallet_label": str(pallet.get("pallet_label") or "-"),
                    "pallet_code": str(pallet.get("pallet_code") or "").strip(),
                    "qr_value": str(pallet.get("qr_value") or "").strip(),
                    "box_count": int(_parse_int(pallet.get("box_count"))),
                    "accepted_scans": accepted_scans,
                }
            )
        rows.append(
            {
                "item": item,
                "order": order,
                "display_number": format_order_number("shipping", order.number),
                "agency_name": _short_agency_name(getattr(order.agency, "agn_name", None)),
                "destination": _order_route_destination(order) or "-",
                "route_position": position,
                "pallets": pallets,
                "pallet_count": len(pallets),
                "box_count": sum(int(pallet["box_count"]) for pallet in pallets),
                "item_count": _shipping_packing_item_count(packing_summary),
                "blocking_error": blocking_error,
            }
        )
    return rows


def _trip_loaded_pallet_keys_from_warehouse(trip: LogisticsTrip, rows: list[dict]) -> list[str]:
    trip_key = str(trip.number or "").strip()
    if not trip_key:
        return []
    loaded_keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        order = row["order"]
        cached_snapshots = getattr(order, "_shipping_flow_snapshots_cache", None)
        if cached_snapshots is not None:
            snapshots = [
                snapshot
                for snapshot in cached_snapshots
                if str(snapshot.current_trip_id or "").strip() == trip_key
                and str(snapshot.warehouse_state_code or "").strip()
                == WarehouseStateCode.LOADED_TO_VEHICLE.value
                and WarehouseWritePathService._snapshot_matches_shipping_context(snapshot, order.number)
            ]
        else:
            snapshots = _warehouse_shipping_snapshots_for_order(
                order,
                trip_id=trip_key,
                state_codes=[WarehouseStateCode.LOADED_TO_VEHICLE.value],
            )
        loaded_pallet_codes = {
            str(snapshot.parent_container.container_code or "").strip().lower()
            for snapshot in snapshots
            if snapshot.parent_container_id and str(snapshot.parent_container.container_code or "").strip()
        }
        if not loaded_pallet_codes:
            continue
        for pallet in row["pallets"]:
            pallet_code = str(pallet.get("pallet_code") or "").strip().lower()
            progress_key = str(pallet.get("progress_key") or "").strip()
            if pallet_code and pallet_code in loaded_pallet_codes and progress_key and progress_key not in seen:
                seen.add(progress_key)
                loaded_keys.append(progress_key)
    return loaded_keys


def _trip_loading_progress(trip: LogisticsTrip, rows: list[dict]) -> dict:
    payload = _trip_loading_payload(trip)
    raw_loaded = payload.get("loaded_pallet_keys") or []
    if not isinstance(raw_loaded, list):
        raw_loaded = []
    known_progress_keys = {
        pallet["progress_key"]
        for row in rows
        for pallet in row["pallets"]
        if pallet["progress_key"]
    }
    progress_keys_by_load_key: dict[str, list[str]] = {}
    for row in rows:
        for pallet in row["pallets"]:
            load_key_norm = str(pallet.get("load_key_norm") or "").strip()
            progress_key = str(pallet.get("progress_key") or "").strip()
            if load_key_norm and progress_key:
                progress_keys_by_load_key.setdefault(load_key_norm, []).append(progress_key)

    loaded_keys: list[str] = []
    loaded_set: set[str] = set()
    for value in raw_loaded:
        key = _normalize_loading_scan(value)
        if not key:
            continue
        if key in known_progress_keys:
            if key not in loaded_keys:
                loaded_keys.append(key)
            loaded_set.add(key)
            continue
        matching_progress_keys = progress_keys_by_load_key.get(key) or []
        if matching_progress_keys:
            if key not in loaded_keys:
                loaded_keys.append(key)
            if len(matching_progress_keys) == 1:
                loaded_set.add(matching_progress_keys[0])
    for key in _trip_loaded_pallet_keys_from_warehouse(trip, rows):
        if key in known_progress_keys:
            loaded_set.add(key)
    current_row = None
    for row in rows:
        loaded_count = 0
        for pallet in row["pallets"]:
            pallet["is_loaded"] = pallet["progress_key"] in loaded_set
            if pallet["is_loaded"]:
                loaded_count += 1
        row["loaded_count"] = loaded_count
        row["remaining_count"] = max(len(row["pallets"]) - loaded_count, 0)
        row["is_complete"] = (
            not row.get("blocking_error")
            and bool(row["pallets"])
            and row["remaining_count"] == 0
        )
        if current_row is None and not row["is_complete"]:
            current_row = row
    return {
        "loaded_keys": loaded_keys,
        "loaded_set": loaded_set,
        "current_row": current_row,
        "all_complete": current_row is None and bool(rows),
        "last_loaded_key": str(payload.get("last_loaded_key") or "").strip(),
    }


def _save_trip_loading_progress(
    trip: LogisticsTrip,
    *,
    loaded_keys: list[str],
    pallet: dict,
    order: ShippingOrder,
    user=None,
) -> None:
    payload = {
        "act": TRIP_LOADING_AUDIT_ACT,
        "trip_pk": trip.pk,
        "loaded_pallet_keys": loaded_keys,
        "last_loaded_key": pallet["load_key_norm"],
        "last_loaded_pallet_label": pallet["pallet_label"],
        "last_loaded_order_number": str(order.number or "").strip(),
        "last_loaded_at": timezone.localtime().isoformat(),
    }
    OrderAuditEntry.objects.create(
        order_id=str(trip.pk),
        order_type=TRIP_LOADING_AUDIT_TYPE,
        action="update",
        agency=order.agency,
        user=user if getattr(user, "is_authenticated", False) else None,
        description=f"Погрузка рейса {_trip_public_number(trip)}: загружена паллета {pallet['pallet_label']}.",
        payload=payload,
    )


def _warehouse_shipping_snapshots_for_order(
    order: ShippingOrder,
    *,
    trip_id: str | None = None,
    state_codes: list[str] | None = None,
) -> list[WarehouseStockSnapshot]:
    cached_snapshots = getattr(order, "_shipping_flow_snapshots_cache", None)
    # An empty flow cache only means the modern event path has no rows. Legacy
    # orders may still match through shipping reserves, so keep the old fallback.
    if cached_snapshots:
        state_set = set(state_codes or [])
        return [
            snapshot
            for snapshot in cached_snapshots
            if (trip_id is None or str(snapshot.current_trip_id or "").strip() == str(trip_id or "").strip())
            and (not state_set or str(snapshot.warehouse_state_code or "").strip() in state_set)
            and WarehouseWritePathService._snapshot_matches_shipping_context(snapshot, order.number)
        ]
    query = WarehouseStockSnapshot.objects.select_related(
        "active_operation",
        "last_event",
        "container",
        "parent_container",
    ).filter(
        agency=order.agency,
        is_archived=False,
    )
    if trip_id is not None:
        query = query.filter(current_trip_id=str(trip_id or "").strip())
    if state_codes:
        query = query.filter(warehouse_state_code__in=list(state_codes))
    return [
        snapshot
        for snapshot in query.order_by("id")
        if WarehouseWritePathService._snapshot_matches_shipping_context(snapshot, order.number)
    ]


def _record_order_loading_from_confirmed_scans(
    trip: LogisticsTrip,
    *,
    order: ShippingOrder,
    user=None,
) -> None:
    trip_key = str(trip.number or "").strip()
    if not trip_key:
        raise ValidationError("У рейса не указан номер.")

    WarehouseWritePathService.validate_shipping_pallet_ownership(
        agency=order.agency,
        order_id=order.number,
        require_pallets=True,
    )

    loaded = _warehouse_shipping_snapshots_for_order(
        order,
        trip_id=trip_key,
        state_codes=[WarehouseStateCode.LOADED_TO_VEHICLE.value],
    )
    if loaded:
        return

    loading = _warehouse_shipping_snapshots_for_order(
        order,
        trip_id=trip_key,
        state_codes=[WarehouseStateCode.LOADING_IN_PROGRESS.value],
    )
    operation = next(
        (
            snapshot.active_operation
            for snapshot in loading
            if snapshot.active_operation is not None
            and str(snapshot.active_operation.operation_type or "").strip() == "load_to_vehicle"
        ),
        None,
    )
    if operation is not None:
        WarehouseWritePathService.complete_loading(operation=operation, performed_by=user)
        return

    assigned = _warehouse_shipping_snapshots_for_order(
        order,
        trip_id=trip_key,
        state_codes=[WarehouseStateCode.ASSIGNED_TO_TRIP.value],
    )
    if not assigned:
        ready = _warehouse_shipping_snapshots_for_order(
            order,
            state_codes=[WarehouseStateCode.READY_FOR_LOADING.value],
        )
        if not ready:
            raise ValidationError(
                f"Заявка {format_order_number('shipping', order.number)} не готова к погрузке."
            )
        WarehouseWritePathService.assign_to_trip(
            agency=order.agency,
            order_id=order.number,
            trip_id=trip_key,
            assigned_by=user,
        )
    operation = WarehouseWritePathService.start_loading(
        agency=order.agency,
        order_id=order.number,
        trip_id=trip_key,
        started_by=user,
    )
    WarehouseWritePathService.complete_loading(operation=operation, performed_by=user)


def _sync_trip_loading_completion_to_warehouse(trip: LogisticsTrip, *, user=None) -> None:
    rows = _trip_loading_rows(trip)
    progress = _trip_loading_progress(trip, rows)
    if not progress["all_complete"]:
        raise ValidationError("Сначала загрузите все паллеты по рейсу.")

    trip_key = str(trip.number or "").strip()
    for row in rows:
        order = row["order"]
        if row.get("blocking_error"):
            raise ValidationError(
                f"Заявка {row['display_number']}: {row['blocking_error']}."
            )
        loaded = _warehouse_shipping_snapshots_for_order(
            order,
            trip_id=trip_key,
            state_codes=[WarehouseStateCode.LOADED_TO_VEHICLE.value],
        )
        try:
            ownership = WarehouseWritePathService.validate_shipping_pallet_ownership(
                agency=order.agency,
                order_id=order.number,
                snapshots=loaded,
                require_pallets=True,
            )
        except ValueError as exc:
            raise ValidationError(
                f"Заявка {row['display_number']}: {exc}"
            ) from exc
        loaded_codes = {
            str(code or "").strip().lower()
            for code in ownership.get("pallet_codes") or []
            if str(code or "").strip()
        }
        expected_codes = {
            str(pallet.get("pallet_code") or "").strip().lower()
            for pallet in row["pallets"]
            if str(pallet.get("pallet_code") or "").strip()
        }
        if loaded_codes != expected_codes or any(not snapshot.is_in_vehicle for snapshot in loaded):
            raise ValidationError(
                f"Заявка {row['display_number']}: факты складской погрузки не совпадают со сканами."
            )


def _ship_trip_orders_after_departure(trip: LogisticsTrip, *, user=None) -> None:
    from shipping.dispatch import confirm_dispatch_act_from_loading
    from shipping.services import (
        ship_order as finalize_shipping_order,
        shipping_loaded_qty_by_item,
    )

    trip_orders = list(
        trip.orders.select_related("shipping_order").order_by("loading_sequence", "id")
    )
    for item in trip_orders:
        order = item.shipping_order
        if not order.items.exists():
            continue
        if order.is_closed():
            continue
        if order.delivery_type == ShippingOrder.DELIVERY_TRANSFER:
            continue
        with transaction.atomic():
            shipped_qty_by_item = shipping_loaded_qty_by_item(
                order,
                trip_number=str(trip.number or "").strip(),
            )
            finalize_shipping_order(
                order,
                user,
                shipped_qty_by_item=shipped_qty_by_item,
            )
            confirm_dispatch_act_from_loading(
                order,
                user,
                packing_summary=_shipping_packing_summary(order),
            )


def _launch_trip_shipping_reconcile(trip: LogisticsTrip) -> None:
    trip_number = str(trip.number or "").strip()
    if not trip_number:
        return
    manage_py = Path(settings.BASE_DIR) / "manage.py"
    if not manage_py.exists():
        logger.error("Cannot launch trip shipping reconcile: manage.py not found at %s", manage_py)
        return
    try:
        subprocess.Popen(
            [sys.executable, str(manage_py), "reconcile_departed_trips", "--trip", trip_number],
            cwd=str(settings.BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        logger.exception("Cannot launch trip shipping reconcile for %s", trip_number)


def _trip_loading_response_payload(trip: LogisticsTrip, rows: list[dict], progress: dict, result: dict | None = None) -> dict:
    current_row = progress["current_row"]
    return {
        "ok": bool(result.get("ok")) if isinstance(result, dict) else True,
        "result": result or {},
        "all_complete": bool(progress["all_complete"]),
        "current_order_number": current_row["display_number"] if current_row else "",
        "current_order_id": current_row["order"].pk if current_row else None,
        "loaded_total": sum(int(row.get("loaded_count") or 0) for row in rows),
        "total_pallets": sum(len(row["pallets"]) for row in rows),
        "rows": [
            {
                "order_id": row["order"].pk,
                "display_number": row["display_number"],
                "agency_name": row["agency_name"],
                "destination": row["destination"],
                "route_position": row["route_position"],
                "loaded_count": row["loaded_count"],
                "pallet_count": row["pallet_count"],
                "is_complete": row["is_complete"],
                "is_current": current_row is not None and row["order"].pk == current_row["order"].pk,
                "pallets": [
                    {
                        "pallet_label": pallet["pallet_label"],
                        "pallet_code": pallet["pallet_code"],
                        "qr_value": pallet["qr_value"],
                        "box_count": pallet["box_count"],
                        "is_loaded": bool(pallet.get("is_loaded")),
                    }
                    for pallet in row["pallets"]
                ],
            }
            for row in rows
        ],
    }


def _trip_loading_document_rows(rows: list[dict]) -> list[dict]:
    document_rows: list[dict] = []
    for row in rows:
        order = row["order"]
        document_rows.append(
            {
                "order_id": order.pk,
                "display_number": row["display_number"],
                "agency_name": row["agency_name"],
                "transport_note_url": f"/shipping/{order.pk}/transport-note/docx/",
                "return_act_url": f"/shipping/{order.pk}/return-act/doc/",
            }
        )
    return document_rows


def _trip_loading_shipping_service_targets(rows: list[dict]) -> list[dict]:
    """Данные отдельных заявок для ввода услуг только после фактической погрузки."""
    targets: list[dict] = []
    for row in rows:
        order = row["order"]
        targets.append(
            {
                "clientId": order.agency_id,
                "clientName": str(order.agency),
                "orderType": "shipping",
                "orderId": order.number,
                "displayNumber": row["display_number"],
                "source": "shipping_loading_manual",
                "items": int(row.get("item_count") or 0),
                "boxes": int(row.get("box_count") or 0),
                "pallets": int(row.get("pallet_count") or 0),
            }
        )
    return targets


def _ensure_trip_storekeeper_task(trip: LogisticsTrip, user=None) -> Task | None:
    if not trip.pk:
        return None
    storekeeper = _first_active_employee_by_roles("storekeeper")
    if not storekeeper:
        return None
    trip_orders = list(
        trip.orders.select_related("shipping_order", "shipping_order__agency").order_by(
            "loading_sequence", "delivery_sequence", "id"
        )
    )
    client_preview = ", ".join(
        list(
            dict.fromkeys(_short_agency_name(getattr(item.shipping_order.agency, "agn_name", None)) for item in trip_orders)
        )[:3]
    ) or "-"
    destinations = ", ".join(
        list(
            dict.fromkeys(
                (_order_route_destination(item.shipping_order) or "-")
                for item in trip_orders
            )
        )[:2]
    ) or "-"
    vehicle_bits = [bit for bit in [trip.vehicle_name.strip(), _format_vehicle_number_for_plate(trip.vehicle_number)] if bit]
    if _trip_is_draft(trip):
        description = (
            f"Логист создал черновик рейса. До формирования рейс доступен кладовщику только для просмотра. "
            f"Клиенты: {client_preview}. "
            f"Склады назначения: {destinations}. "
            f"Транспорт: {' '.join(vehicle_bits) or '-'}. "
            f"Водитель: {trip.driver_name or '-'}, {trip.driver_phone or '-'}."
        )
    else:
        description = (
            f"Рейс сформирован логистом и ожидает машину. "
            f"Клиенты: {client_preview}. "
            f"Склады назначения: {destinations}. "
            f"Транспорт: {' '.join(vehicle_bits) or '-'}. "
            f"Водитель: {trip.driver_name or '-'}, {trip.driver_phone or '-'}."
        )
    route = _trip_route(trip)
    title = _trip_title(trip)
    due_date = timezone.localtime()
    existing = Task.objects.filter(route=route, assigned_to__role="storekeeper").order_by("-created_at").first()
    observer = trip.assigned_logistician
    if existing:
        existing.title = title
        existing.description = description
        existing.assigned_to = storekeeper
        existing.observer = observer
        existing.due_date = due_date
        if existing.status == "done":
            existing.status = "backlog"
        existing.save(
            update_fields=["title", "description", "assigned_to", "observer", "due_date", "status", "updated_at"]
        )
        return existing
    return Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=storekeeper,
        observer=observer,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=due_date,
    )


def _close_trip_tasks_by_role(trip: LogisticsTrip, role: str) -> None:
    Task.objects.filter(route=_trip_route(trip), assigned_to__role=role).exclude(status="done").update(
        status="done",
        updated_at=timezone.now(),
    )


def _ensure_trip_logistician_rework_task(trip: LogisticsTrip, user=None) -> Task | None:
    logistician = trip.assigned_logistician
    if not trip.pk or logistician is None:
        return None
    observer = get_employee_for_user(user) if getattr(user, "is_authenticated", False) else None
    title = _trip_title(trip)
    description = (
        f"Кладовщик вернул рейс {_trip_public_number(trip)} логисту на доработку. "
        f"Проверь состав рейса, маршрут и параметры загрузки перед повторной передачей на погрузку."
    )
    route = _trip_route(trip)
    due_date = timezone.localtime()
    existing = Task.objects.filter(route=route, assigned_to=logistician).order_by("-created_at").first()
    if existing:
        existing.title = title
        existing.description = description
        existing.observer = observer
        existing.due_date = due_date
        if existing.status == "done":
            existing.status = "backlog"
        existing.save(update_fields=["title", "description", "observer", "due_date", "status", "updated_at"])
        return existing
    return Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=logistician,
        observer=observer,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=due_date,
    )


def _trip_delivery_responsible(trip: LogisticsTrip) -> Employee | None:
    responsible = trip.assigned_logistician
    if responsible is None and trip.created_by_id:
        responsible = Employee.objects.filter(user_id=trip.created_by_id, is_active=True).first()
    return responsible or _first_active_employee_by_roles("logistician")


def _ensure_trip_delivery_task(trip: LogisticsTrip, user=None) -> Task | None:
    responsible = _trip_delivery_responsible(trip)
    if not trip.pk or responsible is None:
        return None
    order_count = trip.orders.count()
    title = f"Подтвердить доставку: рейс №{_trip_public_number(trip)}"
    description = (
        f"Погрузка рейса {_trip_public_number(trip)} завершена. "
        f"Отметьте результат доставки по {order_count} заявкам: «Сдано на МП» или «Не сдано»."
    )
    route = _trip_route(trip)
    observer = get_employee_for_user(user) if getattr(user, "is_authenticated", False) else None
    existing = Task.objects.filter(route=route, assigned_to=responsible).order_by("-created_at").first()
    if existing:
        existing.title = title
        existing.description = description
        existing.observer = observer
        existing.due_date = timezone.localtime()
        existing.status = "in_progress"
        existing.save(update_fields=["title", "description", "observer", "due_date", "status", "updated_at"])
        return existing
    return Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=responsible,
        observer=observer,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        status="in_progress",
        due_date=timezone.localtime(),
    )


def _apply_trip_transport_fields(trip: LogisticsTrip, request, *, validate_vehicle: bool = False) -> None:
    from decimal import Decimal, InvalidOperation

    raw_vehicle_number = request.POST.get("vehicle_number")
    raw_driver_phone = request.POST.get("driver_phone")
    trip.vehicle_number = (
        _normalize_vehicle_number(raw_vehicle_number) if validate_vehicle else str(raw_vehicle_number or "").strip()
    )
    trip.driver_name = str(request.POST.get("driver_name") or "").strip()
    trip.driver_phone = _normalize_driver_phone(raw_driver_phone)
    trip.carrier = _resolve_trip_carrier(request)

    def _opt_decimal(name: str):
        raw = str(request.POST.get(name) or "").strip().replace(",", ".")
        if not raw:
            return None
        try:
            return Decimal(raw)
        except (InvalidOperation, ValueError):
            raise ValueError(f"Некорректное значение поля «{name}».")

    if "max_weight_kg" in request.POST:
        trip.max_weight_kg = _opt_decimal("max_weight_kg")
    if "max_volume_m3" in request.POST:
        trip.max_volume_m3 = _opt_decimal("max_volume_m3")
    if "max_pallets" in request.POST:
        raw_pallets = str(request.POST.get("max_pallets") or "").strip()
        trip.max_pallets = int(raw_pallets) if raw_pallets else None


def _active_trip_links(
    order_ids: list[int] | None = None, exclude_trip_id: int | None = None
) -> dict[int, LogisticsTripOrder]:
    qs = (
        LogisticsTripOrder.objects.select_related("trip", "shipping_order")
        .filter(trip__status__in=ACTIVE_TRIP_STATUSES)
        .order_by("-trip__created_at", "-id")
    )
    if order_ids:
        qs = qs.filter(shipping_order_id__in=order_ids)
    if exclude_trip_id is not None:
        qs = qs.exclude(trip_id=exclude_trip_id)
    result: dict[int, LogisticsTripOrder] = {}
    for item in qs:
        result.setdefault(item.shipping_order_id, item)
    return result


def _packing_payload_map(order_numbers: list[str]) -> dict[str, dict]:
    if not order_numbers:
        return {}
    qs = (
        OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id__in=order_numbers,
            payload__act="shipping_packing",
        )
        .order_by("order_id", "-created_at")
    )
    result: dict[str, dict] = {}
    for entry in qs:
        if entry.order_id in result:
            continue
        result[entry.order_id] = dict(entry.payload or {})
    return result


def _prime_trip_shipping_data(trip_orders: list[LogisticsTripOrder]) -> None:
    """Batch-load per-order data used repeatedly while rendering a trip."""
    orders = [item.shipping_order for item in trip_orders]
    order_numbers = {str(order.number or "").strip() for order in orders if str(order.number or "").strip()}
    agency_ids = {int(order.agency_id) for order in orders if order.agency_id}
    snapshots_by_order: dict[tuple[int, str], list[WarehouseStockSnapshot]] = {}
    if order_numbers and agency_ids:
        reserve_statuses = [
            WarehouseReserve.STATUS_ACTIVE,
            WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
            WarehouseReserve.STATUS_ALLOCATED,
            WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
            WarehouseReserve.STATUS_SATISFIED,
        ]
        reserve_orders_by_identity: dict[tuple[int, str, str, str, str], set[str]] = {}
        reserves = WarehouseReserve.objects.filter(
            agency_id__in=agency_ids,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id__in=order_numbers,
            status__in=reserve_statuses,
        ).values_list(
            "agency_id", "context_id", "sku_code", "size", "barcode", "goods_type"
        )
        for agency_id, context_id, sku_code, size, barcode, goods_type in reserves:
            identity = (
                int(agency_id),
                str(sku_code or "").strip(),
                str(size or "").strip(),
                str(barcode or "").strip(),
                str(goods_type or "").strip(),
            )
            reserve_orders_by_identity.setdefault(identity, set()).add(str(context_id or "").strip())

        snapshots = list(
            WarehouseStockSnapshot.objects.select_related(
                "container", "parent_container", "location", "active_operation", "last_event"
            )
            .filter(
                agency_id__in=agency_ids,
                is_archived=False,
                warehouse_state_code__in=[
                    WarehouseStateCode.IN_OTG.value,
                    WarehouseStateCode.PALLETIZING.value,
                    WarehouseStateCode.READY_FOR_LOADING.value,
                    WarehouseStateCode.ASSIGNED_TO_TRIP.value,
                    WarehouseStateCode.LOADING_IN_PROGRESS.value,
                    WarehouseStateCode.LOADED_TO_VEHICLE.value,
                    WarehouseStateCode.SHIPPED.value,
                ],
            )
            .order_by("id")
        )
        parent_ids = {
            int(snapshot.parent_container_id)
            for snapshot in snapshots
            if snapshot.parent_container_id
        }
        sibling_snapshots_by_parent: dict[int, list[WarehouseStockSnapshot]] = {}
        if parent_ids:
            sibling_snapshots = (
                WarehouseStockSnapshot.objects.select_related("active_operation", "last_event")
                .filter(
                    agency_id__in=agency_ids,
                    parent_container_id__in=parent_ids,
                    is_archived=False,
                )
                .order_by("id")
            )
            for sibling in sibling_snapshots:
                sibling_snapshots_by_parent.setdefault(int(sibling.parent_container_id), []).append(sibling)
        for snapshot in snapshots:
            snapshot._shipping_parent_snapshots_cache = sibling_snapshots_by_parent.get(
                int(snapshot.parent_container_id), []
            ) if snapshot.parent_container_id else []
            matched_order_numbers: set[str] = set()
            parent = snapshot.parent_container
            if (
                parent is not None
                and str(parent.source_context_type or "").strip().lower() == "shipping"
                and str(parent.source_context_id or "").strip() in order_numbers
            ):
                matched_order_numbers.add(str(parent.source_context_id or "").strip())
            active_operation = snapshot.active_operation
            if (
                active_operation is not None
                and str(active_operation.context_type or "").strip().lower() == "shipping"
                and str(active_operation.context_id or "").strip() in order_numbers
            ):
                matched_order_numbers.add(str(active_operation.context_id or "").strip())
            last_event = snapshot.last_event
            if (
                last_event is not None
                and str(last_event.stock_context_type or "").strip().lower() == "shipping"
                and str(last_event.stock_context_id or "").strip() in order_numbers
            ):
                matched_order_numbers.add(str(last_event.stock_context_id or "").strip())
            if not matched_order_numbers:
                identity = (
                    int(snapshot.agency_id),
                    str(snapshot.sku_code or "").strip(),
                    str(snapshot.size or "").strip(),
                    str(snapshot.barcode or "").strip(),
                    str(snapshot.goods_type or "").strip(),
                )
                matched_order_numbers.update(reserve_orders_by_identity.get(identity, set()))
            snapshot._shipping_context_ids_cache = matched_order_numbers
            for context_id in matched_order_numbers:
                snapshots_by_order.setdefault((int(snapshot.agency_id), context_id), []).append(snapshot)

    packing_entries: dict[str, OrderAuditEntry] = {}
    if order_numbers:
        entries = (
            OrderAuditEntry.objects.filter(
                order_id__in=order_numbers,
                order_type="shipping",
                payload__act__in=["shipping_packing", "shipping_loose_packing"],
            )
            .order_by("order_id", "-created_at", "-id")
        )
        for entry in entries:
            packing_entries.setdefault(str(entry.order_id), entry)

    for order in orders:
        order_key = str(order.number or "").strip()
        order._shipping_flow_snapshots_cache = snapshots_by_order.get((int(order.agency_id), order_key), [])
        order._shipping_flow_snapshots_cache_complete = True
        order._shipping_packing_entry_cache = packing_entries.get(order_key)


def _dashboard_row_due_date(row: dict):
    order = row["order"]
    if order.slot_date:
        return order.slot_date
    if order.planned_ship_date:
        return order.planned_ship_date
    if order.eta_at:
        return timezone.localtime(order.eta_at).date()
    if order.created_at:
        return timezone.localtime(order.created_at).date()
    return None


def _sort_dashboard_ready_rows(rows: list[dict]) -> list[dict]:
    def sort_moment(row: dict):
        trip_link = row.get("trip_link")
        if trip_link is not None:
            trip = trip_link.trip
            return trip.updated_at or trip.created_at or timezone.now()
        order = row["order"]
        return order.updated_at or order.created_at or timezone.now()

    def sort_key(row: dict):
        trip_link = row.get("trip_link")
        moment = sort_moment(row)
        if trip_link is not None:
            trip = trip_link.trip
            route_rank = int(trip_link.loading_sequence or trip_link.delivery_sequence or 0)
            return (-moment.timestamp(), 0, trip.pk, route_rank, row["order"].pk)
        return (-moment.timestamp(), 1, row["order"].pk)

    return sorted(rows, key=sort_key)


def _readiness_for_dashboard(
    order: ShippingOrder,
    *,
    is_in_trip: bool,
    is_loaded_for_trip: bool,
    trip_link: LogisticsTripOrder | None,
) -> tuple[str, str, str]:
    """Короткий статус для подбора: (key, label, tone)."""
    if order.status == ShippingOrder.STATUS_CANCELED:
        return "cancelled", "Отменено", "muted"
    if is_loaded_for_trip:
        return "shipped", "Отгружено", "muted"
    if is_in_trip and trip_link is not None:
        trip = trip_link.trip
        if trip.status == LogisticsTrip.STATUS_DRAFT or _trip_is_draft(trip):
            return "in_draft", "Добавлено в черновик", "draft"
        return "in_trip", "Рейс сформирован", "trip"
    if order.status == ShippingOrder.STATUS_PACKED:
        return "ready", TRIP_PREPLANNING_STATUS_LABELS[order.status], "ready"
    if order.status == ShippingOrder.STATUS_PICKING:
        return "picking", TRIP_PREPLANNING_STATUS_LABELS[order.status], "warn"
    if order.status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
        return "warehouse_accepted", TRIP_PREPLANNING_STATUS_LABELS[order.status], "prep"
    if order.status == ShippingOrder.STATUS_RESERVED:
        return "warehouse_waiting", TRIP_PREPLANNING_STATUS_LABELS[order.status], "prep"
    if order.status == ShippingOrder.STATUS_SUBMITTED:
        return "manager_waiting", TRIP_PREPLANNING_STATUS_LABELS[order.status], "prep"
    return "not_ready", "Не готово складом", "prep"


def _order_rows(
    orders,
    *,
    active_links: dict[int, LogisticsTripOrder],
    packing_payloads: dict[str, dict],
    movements_by_order_id=None,
) -> list[dict]:
    orders = list(orders)
    if movements_by_order_id is None:
        movements_by_order_id = WarehouseMovementResolver.active_for_shipping_orders(orders)
    rows: list[dict] = []
    for order in orders:
        packing_payload = packing_payloads.get(order.number) or {}
        trip_link = active_links.get(order.id)
        is_in_trip = trip_link is not None
        is_loaded_for_trip = bool(
            trip_link and trip_link.trip.status == LogisticsTrip.STATUS_DEPARTED and order.status == ShippingOrder.STATUS_PACKED
        )
        status_label = _display_shipping_status(
            order,
            is_in_trip=is_in_trip,
            is_loaded_for_trip=is_loaded_for_trip,
            movement=movements_by_order_id.get(order.id),
        )
        readiness_key, readiness_label, readiness_tone = _readiness_for_dashboard(
            order,
            is_in_trip=is_in_trip,
            is_loaded_for_trip=is_loaded_for_trip,
            trip_link=trip_link,
        )
        if is_loaded_for_trip:
            dashboard_stage = "shipped"
            dashboard_subtext = "Отгружено"
        elif order.status in PREPARING_STATUSES:
            dashboard_stage = "preparing"
            dashboard_subtext = readiness_label
        else:
            dashboard_stage = "ready"
            dashboard_subtext = readiness_label if readiness_key != "ready" else "Готово к погрузке"
        mp_obj = getattr(order, "marketplace", None)
        mp = str(getattr(mp_obj, "name", None) or getattr(mp_obj, "code", None) or mp_obj or "").strip()
        warehouse = _order_route_destination(order)
        transit_warehouse = str(order.transit_address or "").strip()
        weight = getattr(order, "cargo_weight_kg", None)
        try:
            weight_kg = float(weight) if weight is not None else 0.0
        except (TypeError, ValueError):
            weight_kg = 0.0
        slot_time = getattr(order, "slot_time", None)
        slot_time_label = slot_time.strftime("%H:%M") if slot_time else ""
        can_select = order.status in TRIP_PREPLANNING_STATUSES and not is_in_trip
        if trip_link and (trip_link.trip.status == LogisticsTrip.STATUS_DRAFT or _trip_is_draft(trip_link.trip)):
            trip_state_label = "Черновик"
        elif trip_link:
            trip_state_label = _trip_public_number(trip_link.trip)
        else:
            trip_state_label = "Не создан"
        rows.append(
            {
                "order": order,
                "display_number": format_order_number("shipping", order.number),
                "agency_name": _short_agency_name(getattr(order.agency, "agn_name", None)),
                "status_label": status_label,
                "status_tone": "shipped" if is_loaded_for_trip else ("default" if is_in_trip else "packed"),
                "readiness_key": readiness_key,
                "readiness_label": readiness_label,
                "readiness_tone": readiness_tone,
                "dashboard_stage": dashboard_stage,
                "dashboard_subtext": dashboard_subtext,
                "box_count": int(packing_payload.get("delivered_box_count") or order.expected_boxes or 0),
                "pallet_count": int(packing_payload.get("pallet_count") or 0),
                "weight_kg": weight_kg,
                "marketplace_label": mp or "—",
                "destination_label": warehouse or "—",
                "transit_label": transit_warehouse,
                "direction_label": " · ".join(part for part in (mp, warehouse) if part) or "—",
                "slot_time_label": slot_time_label,
                "can_select": can_select,
                "trip_link": trip_link,
                "trip_display_number": _trip_public_number(trip_link.trip) if trip_link else "",
                "trip_state_label": trip_state_label,
                "is_in_trip": is_in_trip,
            }
        )
    return rows


def _trip_next_action(*, trip: LogisticsTrip, role: str, resolved_count: int, order_count: int) -> dict:
    detail_url = _trip_route(trip)
    if trip.status == LogisticsTrip.STATUS_DRAFT:
        return {"label": "Продолжить оформление", "url": detail_url, "hint": "Заполните рейс"}
    if trip.status == LogisticsTrip.STATUS_PLANNED:
        return {"label": "Исправить и передать", "url": detail_url, "hint": "Возвращён логисту"}
    if trip.status == LogisticsTrip.STATUS_LOADING:
        if role == "storekeeper":
            return {"label": "Приступить к погрузке", "url": _trip_loading_url(trip), "hint": "Работа склада"}
        return {"label": "Ожидает погрузку", "url": detail_url, "hint": "Ответственный: склад"}
    if trip.status == LogisticsTrip.STATUS_DEPARTED:
        if _can_manage_trip(role):
            return {
                "label": f"Отметить доставку {resolved_count}/{order_count}",
                "url": detail_url,
                "hint": f"Осталось заявок: {max(order_count - resolved_count, 0)}",
            }
        return {"label": "Открыть рейс", "url": detail_url, "hint": "Рейс в пути"}
    if trip.status == LogisticsTrip.STATUS_COMPLETED:
        return {"label": "Открыть итоги", "url": detail_url, "hint": "Рейс завершён"}
    return {"label": "Открыть", "url": detail_url, "hint": ""}


def _trip_rows(limit: int | None = 20, *, role: str = ""):
    trips = (
        LogisticsTrip.objects.select_related("assigned_logistician", "created_by")
        .prefetch_related(
            Prefetch(
                "orders",
                queryset=LogisticsTripOrder.objects.select_related(
                    "shipping_order",
                    "shipping_order__agency",
                    "shipping_order__routing_state",
                ).order_by("loading_sequence", "delivery_sequence", "id"),
            )
        )
        .order_by("-created_at")
    )
    if limit is not None:
        trips = trips[:limit]
    rows: list[dict] = []
    for trip in trips:
        trip_orders = list(trip.orders.all())
        if not trip_orders:
            continue
        client_preview = ", ".join(
            list(
                dict.fromkeys(
                    (_short_agency_name(item.shipping_order.agency.agn_name) or item.shipping_order.number)
                    for item in trip_orders
                )
            )[:3]
        )
        destination_preview = ", ".join(
            list(
                dict.fromkeys(
                    (_order_route_destination(item.shipping_order) or "-")
                    for item in trip_orders
                )
            )[:2]
        )
        resolved_count = 0
        for item in trip_orders:
            try:
                routing_status = item.shipping_order.routing_state.status
            except ShippingRoutingState.DoesNotExist:
                routing_status = ""
            if routing_status in ShippingRoutingState.TERMINAL_DELIVERY_STATUSES:
                resolved_count += 1
        next_action = _trip_next_action(
            trip=trip,
            role=role,
            resolved_count=resolved_count,
            order_count=len(trip_orders),
        )
        rows.append(
            {
                "trip": trip,
                "display_number": _trip_public_number(trip),
                "status_label": _trip_status_label(trip),
                "order_count": len(trip_orders),
                "client_preview": client_preview,
                "destination_preview": destination_preview or "-",
                "driver_name": trip.driver_name or "-",
                "vehicle_name": trip.vehicle_name or "-",
                "vehicle_number_display": _format_vehicle_number_for_plate(trip.vehicle_number) or "-",
                "logistician_name": getattr(trip.assigned_logistician, "full_name", "") or "-",
                "resolved_delivery_count": resolved_count,
                "next_action_label": next_action["label"],
                "next_action_url": next_action["url"],
                "next_action_hint": next_action["hint"],
            }
        )
    return rows


def _trip_addable_statuses(trip: LogisticsTrip) -> list[str]:
    if trip.status in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED}:
        return TRIP_PREPLANNING_STATUSES
    if trip.status == LogisticsTrip.STATUS_LOADING:
        return READY_STATUSES
    return []


def _ready_orders_for_trip(trip: LogisticsTrip):
    from .models import ShippingRoutingState

    allowed_statuses = _trip_addable_statuses(trip)
    qs = (
        ShippingOrder.objects.select_related("agency", "marketplace")
        .filter(status__in=allowed_statuses)
        .exclude(delivery_type__in=TRIP_EXCLUDED_DELIVERY_TYPES)
        .exclude(routing_state__status=ShippingRoutingState.STATUS_CLARIFY)
        .order_by("slot_date", "eta_at", "created_at")
    )
    existing_ids = list(LogisticsTripOrder.objects.filter(trip=trip).values_list("shipping_order_id", flat=True))
    if existing_ids:
        qs = qs.exclude(id__in=existing_ids)
    order_ids = list(qs.values_list("id", flat=True))
    active_links = _active_trip_links(order_ids=order_ids, exclude_trip_id=trip.pk)
    if active_links:
        qs = qs.exclude(id__in=list(active_links.keys()))
    order_numbers = list(qs.values_list("number", flat=True))
    packing_payloads = _packing_payload_map(order_numbers)
    rows = []
    for order in qs:
        packing_payload = packing_payloads.get(order.number) or {}
        rows.append(
            {
                "order": order,
                "display_number": format_order_number("shipping", order.number),
                "agency_name": _short_agency_name(getattr(order.agency, "agn_name", None)),
                "box_count": int(packing_payload.get("delivered_box_count") or order.expected_boxes or 0),
                "pallet_count": int(packing_payload.get("pallet_count") or 0),
            }
        )
    return rows


def _available_orders_for_trip_context(trip: LogisticsTrip, *, can_add_orders: bool) -> list[dict]:
    if not can_add_orders:
        return []
    return _ready_orders_for_trip(trip)


def build_logistics_dashboard_context(*, role: str, employee=None) -> dict:
    preparing_orders = list(
        ShippingOrder.objects.select_related("agency", "marketplace")
        .filter(status__in=PREPARING_STATUSES)
        .exclude(delivery_type__in=TRIP_EXCLUDED_DELIVERY_TYPES)
        .order_by("slot_date", "eta_at", "created_at")
    )
    ready_orders = list(
        ShippingOrder.objects.select_related("agency", "marketplace")
        .filter(status__in=READY_STATUSES)
        .exclude(delivery_type__in=TRIP_EXCLUDED_DELIVERY_TYPES)
        .order_by("slot_date", "eta_at", "created_at")
    )
    all_orders = preparing_orders + ready_orders
    active_links = _active_trip_links(order_ids=[order.id for order in all_orders])
    packing_payloads = _packing_payload_map([order.number for order in all_orders])
    movements_by_order_id = WarehouseMovementResolver.active_for_shipping_orders(all_orders)
    trip_rows = _trip_rows(role=role)
    draft_trips = [row["trip"] for row in trip_rows if row["trip"].status == LogisticsTrip.STATUS_DRAFT]
    if role == "logistician":
        draft_trips = [
            trip
            for trip in draft_trips
            if employee is not None and trip.assigned_logistician_id == employee.pk
        ]
    sidebar_trip = draft_trips[0] if draft_trips else None
    preparing_rows = _order_rows(
        preparing_orders,
        active_links=active_links,
        packing_payloads=packing_payloads,
        movements_by_order_id=movements_by_order_id,
    )
    ready_rows = _sort_dashboard_ready_rows(
        _order_rows(
            ready_orders,
            active_links=active_links,
            packing_payloads=packing_payloads,
            movements_by_order_id=movements_by_order_id,
        )
    )
    ready_for_trip_count = sum(
        1 for row in ready_rows + preparing_rows if row.get("can_select")
    )
    marketplace_options = sorted(
        {
            str(row["marketplace_label"]).strip()
            for row in ready_rows + preparing_rows
            if row.get("marketplace_label") and row["marketplace_label"] != "—"
        }
    )
    warehouse_options = sorted(
        {
            str(row["destination_label"]).strip()
            for row in ready_rows + preparing_rows
            if row.get("destination_label") and row["destination_label"] != "—"
        }
    )
    pool_total_count = len(ready_rows) + len(preparing_rows)
    return {
        "role": role,
        "cabinet_url": resolve_cabinet_url(role),
        "preparing_orders": preparing_rows,
        "ready_orders": ready_rows,
        "trips": trip_rows,
        "sidebar_trip": sidebar_trip,
        "draft_trip_count": len(draft_trips),
        "ready_for_trip_count": ready_for_trip_count,
        "preparing_count": len(preparing_rows),
        "pool_total_count": pool_total_count,
        "marketplace_options": marketplace_options,
        "warehouse_options": warehouse_options,
        "trip_vehicle_choices": LogisticsTrip.VEHICLE_CHOICES,
        "is_logistician": role == "logistician",
        **_shell_context(role, trips_subnav="pool"),
    }


def _selected_preplanning_orders_from_post(request):
    selected_ids = [_parse_int(value) for value in request.POST.getlist("order_ids") if _parse_int(value) > 0]
    if not selected_ids:
        return [], "Выберите хотя бы одну согласованную заявку для планирования рейса."
    selected_orders = list(
        ShippingOrder.objects.select_related("agency")
        .filter(id__in=selected_ids, status__in=TRIP_PREPLANNING_STATUSES)
        .exclude(delivery_type__in=TRIP_EXCLUDED_DELIVERY_TYPES)
        .order_by("slot_date", "eta_at", "created_at")
    )
    if len(selected_orders) != len(set(selected_ids)):
        return [], "Часть заявок недоступна для консолидации в рейс."
    active_links = _active_trip_links(order_ids=[order.id for order in selected_orders])
    if active_links:
        linked_orders = ", ".join(active_links[item_id].shipping_order.number for item_id in active_links)
        return [], f"Некоторые заявки уже включены в активные рейсы: {linked_orders}."
    return selected_orders, ""


def handle_logistics_dashboard_post(request, *, role: str, employee=None):
    action = (request.POST.get("action") or "").strip()
    if action not in {"create_trip", "add_to_draft"}:
        return None

    selected_orders, error = _selected_preplanning_orders_from_post(request)
    if error:
        messages.error(request, error)
        return redirect("logistics:dashboard")

    if action == "add_to_draft":
        draft_id = _parse_int(request.POST.get("draft_trip_id"))
        draft = LogisticsTrip.objects.filter(
            pk=draft_id,
            status=LogisticsTrip.STATUS_DRAFT,
        ).first()
        if draft is None:
            messages.error(request, "Черновик рейса не найден. Сначала создайте рейс.")
            return redirect("logistics:dashboard")
        if role == "logistician" and (
            employee is None or draft.assigned_logistician_id != employee.pk
        ):
            return HttpResponseForbidden("Доступ к чужому черновику запрещен")
        existing_ids = set(draft.orders.values_list("shipping_order_id", flat=True))
        start_position = draft.orders.count()
        created = 0
        for order in selected_orders:
            if order.id in existing_ids:
                continue
            start_position += 1
            LogisticsTripOrder.objects.create(
                trip=draft,
                shipping_order=order,
                loading_sequence=start_position,
                delivery_sequence=start_position,
            )
            created += 1
        if created:
            _ensure_trip_storekeeper_task(draft, request.user)
            messages.success(request, f"В черновик добавлено заявок: {created}.")
        else:
            messages.error(request, "Новые заявки для черновика не найдены.")
        return redirect("logistics:trip-detail", pk=draft.pk)

    try:
        trip = LogisticsTrip(
            number=next_draft_trip_number(),
            trip_date=_parse_date(request.POST.get("trip_date")),
            status=LogisticsTrip.STATUS_DRAFT,
            vehicle_type=str(request.POST.get("vehicle_type") or "").strip(),
            vehicle_name=str(request.POST.get("vehicle_name") or "").strip(),
            route_comment=str(request.POST.get("route_comment") or "").strip(),
            assigned_logistician=employee if employee and employee.role == "logistician" else None,
            created_by=request.user if request.user.is_authenticated else None,
        )
        _apply_trip_transport_fields(trip, request, validate_vehicle=False)
        trip.save()
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("logistics:dashboard")
    for index, order in enumerate(selected_orders, start=1):
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=index,
            delivery_sequence=index,
        )
    _ensure_trip_storekeeper_task(trip, request.user)
    messages.success(request, "Создан черновик рейса.")
    return redirect("logistics:trip-detail", pk=trip.pk)


def build_logistics_trip_list_context(*, role: str, bucket: str = "") -> dict:
    bucket = (bucket or "").strip().lower()
    if bucket == "completed":
        subnav = "completed"
    elif bucket == "active":
        subnav = "active"
    else:
        subnav = "list"
    return {
        "role": role,
        "cabinet_url": resolve_cabinet_url(role),
        "trips": _trip_rows(limit=None, role=role),
        "trips_bucket": bucket,
        **_shell_context(role, trips_subnav=subnav),
    }


def get_trip_detail_trip(pk: int) -> LogisticsTrip:
    return get_object_or_404(
        LogisticsTrip.objects.select_related(
            "assigned_logistician",
            "created_by",
            "carrier",
            "last_edited_by",
            "editing_by",
        ).prefetch_related(
            Prefetch(
                "orders",
                queryset=LogisticsTripOrder.objects.select_related(
                    "shipping_order",
                    "shipping_order__agency",
                    "shipping_order__marketplace",
                    "shipping_order__routing_state",
                ).order_by(
                    "loading_sequence", "delivery_sequence", "id"
                ),
            )
        ),
        pk=pk,
    )


def build_logistics_service_targets(trip: LogisticsTrip, *, order_rows=None) -> list[dict]:
    """Сводка рейса по клиентам для последовательного ввода оказанных услуг."""
    if trip.trip_kind == LogisticsTrip.KIND_EXTERNAL:
        details = getattr(trip, "external_details", None)
        if not details or not details.client_id:
            return []
        return [
            {
                "clientId": details.client_id,
                "clientName": str(details.client),
                "orderType": "logistics",
                "orderId": trip.number,
                "source": "logistics_manual",
                "items": 1,
                "boxes": 0,
                "pallets": 0,
            }
        ]

    if order_rows is None:
        links = list(
            trip.orders.select_related("shipping_order__agency").order_by(
                "delivery_sequence", "id"
            )
        )
        packing_payloads = _packing_payload_map(
            [link.shipping_order.number for link in links]
        )
        order_rows = []
        for link in links:
            order = link.shipping_order
            payload = packing_payloads.get(order.number) or {}
            order_rows.append(
                {
                    "order": order,
                    "box_count": int(payload.get("delivered_box_count") or order.expected_boxes or 0),
                    "pallet_count": int(payload.get("pallet_count") or 0),
                }
            )

    grouped: dict[int, dict] = {}
    for row in order_rows:
        order = row.get("order")
        client = getattr(order, "agency", None)
        if not client or not client.pk:
            continue
        target = grouped.setdefault(
            client.pk,
            {
                "clientId": client.pk,
                "clientName": str(client),
                "orderType": "logistics",
                "orderId": trip.number,
                "source": "logistics_manual",
                "items": 0,
                "boxes": 0,
                "pallets": 0,
            },
        )
        target["items"] += 1
        target["boxes"] += int(row.get("box_count") or 0)
        target["pallets"] += int(row.get("pallet_count") or 0)
    return list(grouped.values())


def build_logistics_trip_detail_context(*, role: str, trip: LogisticsTrip, employee=None) -> dict:
    can_edit_trip = _can_edit_trip(role, trip)
    can_fill_missing_transport = _can_fill_missing_trip_transport(role, trip)
    can_add_orders_to_trip = _can_add_orders_to_trip(role, trip)
    can_remove_orders_from_trip = _can_remove_orders_from_trip(role, trip)
    can_return_trip = _can_return_trip(role, trip)
    can_begin_loading = _can_begin_loading(role, trip)
    can_mark_mp_delivery = _can_mark_mp_delivery(role, trip)
    trip_orders = list(trip.orders.all())
    _prime_trip_shipping_data(trip_orders)
    movements_by_order_id = WarehouseMovementResolver.active_for_shipping_orders(
        [item.shipping_order for item in trip_orders]
    )
    packing_payloads = _packing_payload_map([item.shipping_order.number for item in trip_orders])
    loading_rows = _trip_loading_rows(trip)
    loading_progress = _trip_loading_progress(trip, loading_rows)
    loaded_order_ids = {row["order"].pk for row in loading_rows if row.get("is_complete")} if loading_progress["loaded_set"] else set()
    warehouse_ready_count = sum(
        1 for item in trip_orders if item.shipping_order.status == ShippingOrder.STATUS_PACKED
    )
    pallet_blockers: list[tuple[ShippingOrder, str]] = []
    order_rows = []
    for index, item in enumerate(trip_orders, start=1):
        order = item.shipping_order
        packing_payload = packing_payloads.get(order.number) or {}
        routing = getattr(order, "routing_state", None)
        routing_status = getattr(routing, "status", "") or ""
        if routing_status == ShippingRoutingState.STATUS_DELIVERED:
            delivery_outcome = "delivered"
            delivery_label = "Сдано на МП"
        elif routing_status == ShippingRoutingState.STATUS_FAILED:
            delivery_outcome = "failed"
            delivery_label = "Не сдано"
        else:
            delivery_outcome = ""
            delivery_label = "Ожидает отметки"
        pallet_error = ""
        if order.status == ShippingOrder.STATUS_PACKED:
            pallet_error = _shipping_trip_pallet_error(order, packing_payload)
            if pallet_error:
                pallet_blockers.append((order, pallet_error))
        order_rows.append(
            {
                "item": item,
                "order": order,
                "route_position": index,
                "display_number": format_order_number("shipping", order.number),
                "agency_name": _short_agency_name(getattr(order.agency, "agn_name", None)),
                "status_label": _display_shipping_status(
                    order,
                    is_in_trip=True,
                    is_loaded_for_trip=order.pk in loaded_order_ids,
                    movement=movements_by_order_id.get(order.pk),
                ),
                "planning_status_label": TRIP_PREPLANNING_STATUS_LABELS.get(
                    order.status,
                    order.get_status_display(),
                ),
                "box_count": int(packing_payload.get("delivered_box_count") or order.expected_boxes or 0),
                "pallet_count": int(packing_payload.get("pallet_count") or 0),
                "delivery_outcome": delivery_outcome,
                "delivery_label": delivery_label,
                "can_mark_delivery": can_mark_mp_delivery and not delivery_outcome,
                "can_remove_from_trip": can_remove_orders_from_trip and _shipping_order_can_be_removed_from_trip(order),
            }
        )
    unresolved_rows = [row for row in order_rows if not row["delivery_outcome"]]
    if len(unresolved_rows) == 1:
        unresolved_rows[0]["will_complete_trip"] = True
    can_finalize_trip = can_edit_trip and trip.status in {
        LogisticsTrip.STATUS_DRAFT,
        LogisticsTrip.STATUS_PLANNED,
    }
    trip_chat_url = ""
    try:
        from client_cabinet.chat_trips import ensure_trip_thread, trip_chat_url as _trip_chat_url

        ensure_trip_thread(trip, user=getattr(employee, "user", None))
        trip_chat_url = _trip_chat_url(trip)
    except Exception:
        trip_chat_url = "/team-manager/chats/?kind=logistics"

    context = {
        "role": role,
        "cabinet_url": resolve_cabinet_url(role),
        "can_edit_trip": can_edit_trip,
        "can_fill_missing_transport": can_fill_missing_transport,
        "can_edit_vehicle_number": can_edit_trip or (can_fill_missing_transport and not trip.vehicle_number.strip()),
        "can_edit_driver_name": can_edit_trip or (can_fill_missing_transport and not trip.driver_name.strip()),
        "can_edit_driver_phone": can_edit_trip or (can_fill_missing_transport and not trip.driver_phone.strip()),
        "can_add_orders_to_trip": can_add_orders_to_trip,
        "can_remove_orders_from_trip": can_remove_orders_from_trip,
        "can_return_trip": can_return_trip,
        "can_begin_loading": can_begin_loading,
        "can_start_loading": _can_start_loading(role, trip),
        "show_loading_entrypoint": can_begin_loading,
        "can_finalize_trip": can_finalize_trip,
        "can_mark_mp_delivery": can_mark_mp_delivery,
        "trip": trip,
        "trip_display_number": _trip_public_number(trip),
        "trip_chat_url": trip_chat_url,
        "trip_loading_url": _trip_loading_url(trip),
        "trip_is_departed": trip.status == LogisticsTrip.STATUS_DEPARTED,
        "trip_is_completed": trip.status == LogisticsTrip.STATUS_COMPLETED,
        "trip_status_choices": LogisticsTrip.STATUS_CHOICES,
        "trip_vehicle_choices": LogisticsTrip.VEHICLE_CHOICES,
        "trip_orders": order_rows,
        "trip_order_count": len(order_rows),
        "trip_warehouse_ready_count": warehouse_ready_count,
        "trip_warehouse_ready_for_finalize": bool(order_rows)
        and warehouse_ready_count == len(order_rows)
        and not pallet_blockers,
        "trip_total_pallets": sum(row["pallet_count"] for row in order_rows),
        "trip_total_boxes": sum(row["box_count"] for row in order_rows),
        "trip_service_targets": build_logistics_service_targets(trip, order_rows=order_rows),
        "wsfm_order_type": "logistics",
        "wsfm_order_id": trip.number,
        "wsfm_client_id": "",
        "wsfm_source": "logistics_manual",
        "wsfm_title": "Фактически оказанные услуги рейса",
        "wsfm_items_label": "Заявок / направлений",
        "wsfm_cancel_label": "Вернуться к рейсу",
        "trip_vehicle_number_display": _format_vehicle_number_for_plate(trip.vehicle_number),
        "carrier_options": list(_carrier_options_queryset(exclude_trip_id=trip.pk)),
        "shipper_name": _trip_shipper_name(),
        "customer_name": _trip_customer_summary(trip_orders),
        "consignee_name": _trip_consignee_summary(trip_orders),
        "available_ready_orders": _available_orders_for_trip_context(
            trip,
            can_add_orders=can_add_orders_to_trip,
        ),
        "status_label": _trip_status_label(trip),
        "trip_validation_blocking": [],
        "trip_validation_warnings": [],
        "trip_validation_issues": [],
        "trip_validation_highlight_fields": [],
        "trip_cargo_totals": {},
        "trip_can_finalize_checks_ok": True,
        **_shell_context(role, trips_subnav="list"),
    }
    try:
        from logistics.trip_concurrency import touch_trip_presence, trip_concurrency_context

        # Сначала фиксируем, кто уже был на карточке, затем отмечаем своё присутствие.
        concurrency = trip_concurrency_context(trip, employee)
        if can_edit_trip:
            touch_trip_presence(trip, employee)
        context.update(concurrency)
    except Exception:
        context.setdefault("trip_edit_version", int(getattr(trip, "edit_version", 1) or 1))
        context.setdefault("trip_last_edited_label", "")
        context.setdefault("trip_concurrent_editor_label", "")
    try:
        from logistics.trip_validation import TripValidationIssue, validate_trip_for_finalize

        order_numbers = [row["item"].shipping_order.number for row in order_rows if row.get("item")]
        # Validation is only meaningful while logistician can still finalize.
        # After departure/loading, "order shipped" / vehicle conflicts look like bugs.
        if can_finalize_trip:
            validation = validate_trip_for_finalize(trip, packing_payloads=packing_payloads)
            for order, pallet_error in pallet_blockers:
                validation.issues.append(
                    TripValidationIssue(
                        "pallet_ownership",
                        f"Невозможно утвердить рейс: заявка "
                        f"{format_order_number('shipping', order.number)} заблокирована: "
                        f"{pallet_error}.",
                        field="orders",
                    )
                )
            payload = validation.as_ui_payload()
            context["trip_validation_blocking"] = validation.blocking_messages
            context["trip_validation_warnings"] = validation.warning_messages
            context["trip_validation_issues"] = payload["blocking"] + payload["warnings"]
            context["trip_validation_highlight_fields"] = payload["highlight_fields"]
            context["trip_cargo_totals"] = validation.totals
            context["trip_can_finalize_checks_ok"] = validation.ok
        else:
            # Still show cargo totals for reference on locked trips.
            from logistics.trip_validation import collect_trip_cargo_totals

            context["trip_validation_blocking"] = []
            context["trip_validation_warnings"] = []
            context["trip_validation_issues"] = []
            context["trip_validation_highlight_fields"] = []
            context["trip_cargo_totals"] = collect_trip_cargo_totals(
                trip, packing_payloads=packing_payloads
            )
            context["trip_can_finalize_checks_ok"] = True
    except Exception:
        context["trip_validation_blocking"] = []
        context["trip_validation_warnings"] = []
        context["trip_validation_issues"] = []
        context["trip_validation_highlight_fields"] = []
        context["trip_cargo_totals"] = {}
        context["trip_can_finalize_checks_ok"] = True
    return context


def _trip_post_is_ajax(request) -> bool:
    return (request.headers.get("X-Requested-With") or "") == "XMLHttpRequest"


def _trip_mutation_response(request, *, trip: LogisticsTrip, success_message: str = "", conflict: Exception | None = None):
    if conflict is not None:
        if _trip_post_is_ajax(request):
            return JsonResponse(
                {
                    "ok": False,
                    "conflict": True,
                    "message": str(conflict),
                    "edit_version": int(getattr(trip, "edit_version", 1) or 1),
                },
                status=409,
            )
        messages.error(request, str(conflict))
        return redirect("logistics:trip-detail", pk=trip.pk)
    if _trip_post_is_ajax(request):
        return JsonResponse(
            {
                "ok": True,
                "edit_version": int(getattr(trip, "edit_version", 1) or 1),
                "message": success_message,
            }
        )
    if success_message:
        messages.success(request, success_message)
    return redirect("logistics:trip-detail", pk=trip.pk)


def handle_logistics_trip_detail_post(request, *, role: str, trip: LogisticsTrip):
    from logistics.trip_concurrency import (
        TripEditConflict,
        bump_trip_edit_version,
        parse_expected_version,
    )

    can_edit_trip = _can_edit_trip(role, trip)
    can_fill_missing_transport = _can_fill_missing_trip_transport(role, trip)
    can_add_orders_to_trip = _can_add_orders_to_trip(role, trip)
    can_remove_orders_from_trip = _can_remove_orders_from_trip(role, trip)
    can_return_trip = _can_return_trip(role, trip)
    can_begin_loading = _can_begin_loading(role, trip)
    editor = get_employee_for_user(request.user) if getattr(request.user, "is_authenticated", False) else None
    expected_version = parse_expected_version(request.POST.get("edit_version"))
    action = (request.POST.get("action") or "").strip()
    if action == "start_loading":
        if not can_begin_loading:
            return HttpResponseForbidden("Доступ запрещен")
        loading_rows = _trip_loading_rows(trip)
        blocked_row = next((row for row in loading_rows if row.get("blocking_error")), None)
        if blocked_row is not None:
            messages.error(
                request,
                f"Заявка {blocked_row['display_number']} заблокирована: "
                f"{blocked_row['blocking_error']}.",
            )
            return redirect("logistics:trip-detail", pk=trip.pk)
        return redirect("logistics:trip-loading", pk=trip.pk)
    if action == "return_to_logistic":
        if not can_return_trip:
            return HttpResponseForbidden("Доступ запрещен")
        try:
            bump_trip_edit_version(
                trip,
                expected_version=expected_version,
                editor=editor,
                extra_updates={"status": LogisticsTrip.STATUS_PLANNED},
            )
        except TripEditConflict as exc:
            return _trip_mutation_response(request, trip=trip, conflict=exc)
        _close_trip_tasks_by_role(trip, "storekeeper")
        _ensure_trip_logistician_rework_task(trip, request.user)
        messages.success(request, f"Рейс {_trip_public_number(trip)} возвращен логисту на доработку.")
        return redirect("logistics:trip-detail", pk=trip.pk)
    if action in {"mark_delivered", "mark_not_delivered"}:
        if not _can_mark_mp_delivery(role, trip):
            return HttpResponseForbidden("Доступ запрещен")
        order_id = _parse_int(request.POST.get("shipping_order_id"))
        link = trip.orders.select_related("shipping_order").filter(shipping_order_id=order_id).first()
        if link is None:
            messages.error(request, "Заявка не найдена в этом рейсе.")
            return redirect("logistics:trip-detail", pk=trip.pk)
        order = link.shipping_order
        from logistics.routing_services import (
            delivery_outcome_will_complete_trip,
            mark_order_delivered,
            mark_order_delivery_failed,
        )

        try:
            with transaction.atomic():
                locked_trip = LogisticsTrip.objects.select_for_update().get(pk=trip.pk)
                locked_order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
                if delivery_outcome_will_complete_trip(locked_trip, locked_order):
                    from billing.warehouse_services import require_logistics_trip_completion_facts

                    require_logistics_trip_completion_facts(locked_trip)
                if action == "mark_delivered":
                    _ship_single_trip_order(locked_order, user=request.user)
                    mark_order_delivered(locked_order, user=request.user)
                    messages.success(request, f"Заявка {locked_order.number}: сдана на МП.")
                else:
                    mark_order_delivery_failed(locked_order, user=request.user)
                    messages.success(request, f"Заявка {locked_order.number}: не сдана на МП.")
        except Exception as exc:
            messages.error(request, f"Не удалось отметить сдачу: {exc}")
            return redirect("logistics:trip-detail", pk=trip.pk)
        trip.refresh_from_db(fields=["status"])
        if trip.status == LogisticsTrip.STATUS_COMPLETED:
            messages.success(
                request,
                f"Рейс {_trip_public_number(trip)} завершён — по всем заявкам есть исход сдачи.",
            )
        return redirect("logistics:trip-detail", pk=trip.pk)
    if action == "add_orders":
        if not can_add_orders_to_trip:
            return HttpResponseForbidden("Доступ запрещен")
        selected_ids = [_parse_int(value) for value in request.POST.getlist("order_ids") if _parse_int(value) > 0]
        if not selected_ids:
            messages.error(request, "Выберите заявки для добавления в рейс.")
            return redirect("logistics:trip-detail", pk=trip.pk)
        active_links = _active_trip_links(order_ids=selected_ids, exclude_trip_id=trip.pk)
        if active_links:
            linked_orders = ", ".join(active_links[item_id].shipping_order.number for item_id in active_links)
            messages.error(request, f"Некоторые заявки уже включены в другие активные рейсы: {linked_orders}.")
            return redirect("logistics:trip-detail", pk=trip.pk)
        existing_ids = set(trip.orders.values_list("shipping_order_id", flat=True))
        start_position = trip.orders.count()
        allowed_statuses = _trip_addable_statuses(trip)
        candidates = list(
            ShippingOrder.objects.select_related("agency")
            .filter(id__in=selected_ids, status__in=allowed_statuses)
            .exclude(delivery_type__in=TRIP_EXCLUDED_DELIVERY_TYPES)
            .order_by("slot_date", "eta_at", "created_at")
        )
        if len(candidates) != len(set(selected_ids)):
            messages.error(request, "Часть заявок недоступна для добавления в этот рейс.")
            return redirect("logistics:trip-detail", pk=trip.pk)
        if trip.status == LogisticsTrip.STATUS_LOADING:
            for order in candidates:
                pallet_error = _shipping_trip_pallet_error(order)
                if pallet_error:
                    messages.error(
                        request,
                        f"Заявка {format_order_number('shipping', order.number)} заблокирована: "
                        f"{pallet_error}.",
                    )
                    return redirect("logistics:trip-detail", pk=trip.pk)
        try:
            bump_trip_edit_version(trip, expected_version=expected_version, editor=editor)
        except TripEditConflict as exc:
            return _trip_mutation_response(request, trip=trip, conflict=exc)
        created = 0
        for order in candidates:
            if order.id in existing_ids:
                continue
            start_position += 1
            LogisticsTripOrder.objects.create(
                trip=trip,
                shipping_order=order,
                loading_sequence=start_position,
                delivery_sequence=start_position,
            )
            if trip.status == LogisticsTrip.STATUS_LOADING:
                try:
                    from logistics.routing_services import sync_routing_from_trip

                    sync_routing_from_trip(order)
                except Exception:
                    pass
            created += 1
        if created:
            _ensure_trip_storekeeper_task(trip, request.user)
            messages.success(request, f"В рейс добавлено заявок: {created}.")
        else:
            messages.error(request, "Новые заявки для добавления не найдены.")
        return redirect("logistics:trip-detail", pk=trip.pk)
    if action == "remove_order":
        if not can_remove_orders_from_trip:
            return HttpResponseForbidden("Доступ запрещен")
        link_id = _parse_int(request.POST.get("trip_order_id"))
        link = trip.orders.select_related("shipping_order").filter(id=link_id).first()
        if link is None:
            messages.error(request, "Связь рейса с заявкой не найдена.")
            return redirect("logistics:trip-detail", pk=trip.pk)
        order = link.shipping_order
        if not _shipping_order_can_be_removed_from_trip(order):
            status_label = SHIPPING_STATUS_LABELS.get(order.status, order.get_status_display())
            messages.error(
                request,
                f"Заявку {order.number} нельзя снять с рейса: текущий статус — {status_label}.",
            )
            return redirect("logistics:trip-detail", pk=trip.pk)
        try:
            bump_trip_edit_version(trip, expected_version=expected_version, editor=editor)
        except TripEditConflict as exc:
            return _trip_mutation_response(request, trip=trip, conflict=exc)
        link.delete()
        try:
            from logistics.routing_services import sync_routing_from_trip

            sync_routing_from_trip(order)
        except Exception:
            pass
        if not trip.orders.exists() and trip.status == LogisticsTrip.STATUS_DRAFT:
            Task.objects.filter(
                route=_trip_route(trip),
                assigned_to__role="storekeeper",
            ).delete()
            trip.delete()
            messages.success(request, "Пустой черновик рейса удален.")
            return redirect("logistics:trip-list")
        if trip.orders.exists():
            _ensure_trip_storekeeper_task(trip, request.user)
            messages.success(request, "Заявка удалена из рейса.")
        else:
            messages.success(request, "Заявка удалена из рейса. Рейс остался пустым — добавьте заявку или отмените рейс.")
        return redirect("logistics:trip-detail", pk=trip.pk)
    if action == "update_loading_params" and not can_edit_trip:
        if not can_fill_missing_transport:
            return HttpResponseForbidden("Доступ запрещен")
        try:
            updates = {}
            if not trip.vehicle_number.strip() and "vehicle_number" in request.POST:
                updates["vehicle_number"] = _normalize_vehicle_number(request.POST.get("vehicle_number"))
            if not trip.driver_name.strip() and "driver_name" in request.POST:
                updates["driver_name"] = str(request.POST.get("driver_name") or "").strip()
            if not trip.driver_phone.strip() and "driver_phone" in request.POST:
                updates["driver_phone"] = _normalize_driver_phone(request.POST.get("driver_phone"))
            if updates:
                bump_trip_edit_version(
                    trip,
                    expected_version=expected_version,
                    editor=editor,
                    extra_updates=updates,
                )
        except TripEditConflict as exc:
            return _trip_mutation_response(request, trip=trip, conflict=exc)
        except ValueError as exc:
            if _trip_post_is_ajax(request):
                return JsonResponse({"ok": False, "message": str(exc)}, status=400)
            messages.error(request, str(exc))
            return redirect("logistics:trip-detail", pk=trip.pk)
        return _trip_mutation_response(
            request,
            trip=trip,
            success_message="Отсутствующие данные машины и водителя сохранены.",
        )
    if not can_edit_trip:
        return HttpResponseForbidden("Доступ запрещен")
    if action == "update_loading_params":
        try:
            _apply_trip_transport_fields(trip, request, validate_vehicle=True)
            trip.trip_date = _parse_date(request.POST.get("trip_date"))
            bump_trip_edit_version(
                trip,
                expected_version=expected_version,
                editor=editor,
                extra_updates={
                    "trip_date": trip.trip_date,
                    "vehicle_number": trip.vehicle_number,
                    "driver_name": trip.driver_name,
                    "driver_phone": trip.driver_phone,
                    "carrier": trip.carrier,
                    "max_weight_kg": trip.max_weight_kg,
                    "max_volume_m3": trip.max_volume_m3,
                    "max_pallets": trip.max_pallets,
                },
            )
        except TripEditConflict as exc:
            return _trip_mutation_response(request, trip=trip, conflict=exc)
        except ValueError as exc:
            if _trip_post_is_ajax(request):
                return JsonResponse({"ok": False, "message": str(exc)}, status=400)
            messages.error(request, str(exc))
            return redirect("logistics:trip-detail", pk=trip.pk)
        return _trip_mutation_response(request, trip=trip, success_message="Параметры загрузки обновлены.")
    if action == "update_trip":
        try:
            requested_status = str(request.POST.get("status") or trip.status).strip() or trip.status
            if requested_status != trip.status:
                raise ValueError("Статус рейса изменяется только предусмотренным действием.")
            trip.trip_date = _parse_date(request.POST.get("trip_date"))
            trip.vehicle_type = str(request.POST.get("vehicle_type") or "").strip()
            trip.vehicle_name = str(request.POST.get("vehicle_name") or "").strip()
            _apply_trip_transport_fields(trip, request, validate_vehicle=True)
            trip.route_comment = str(request.POST.get("route_comment") or "").strip()
            trip.loading_comment = str(request.POST.get("loading_comment") or "").strip()
            bump_trip_edit_version(
                trip,
                expected_version=expected_version,
                editor=editor,
                extra_updates={
                    "trip_date": trip.trip_date,
                    "vehicle_type": trip.vehicle_type,
                    "vehicle_name": trip.vehicle_name,
                    "vehicle_number": trip.vehicle_number,
                    "driver_name": trip.driver_name,
                    "driver_phone": trip.driver_phone,
                    "carrier": trip.carrier,
                    "max_weight_kg": trip.max_weight_kg,
                    "max_volume_m3": trip.max_volume_m3,
                    "max_pallets": trip.max_pallets,
                    "route_comment": trip.route_comment,
                    "loading_comment": trip.loading_comment,
                },
            )
        except TripEditConflict as exc:
            return _trip_mutation_response(request, trip=trip, conflict=exc)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("logistics:trip-detail", pk=trip.pk)
        return _trip_mutation_response(request, trip=trip, success_message="Параметры рейса обновлены.")
    if action == "finalize_trip":
        try:
            _apply_trip_transport_fields(trip, request, validate_vehicle=True)
            trip.trip_date = _parse_date(request.POST.get("trip_date"))
            if not trip.driver_name:
                raise ValueError("Укажите ФИО водителя перед формированием рейса.")
            if trip.orders.count() <= 0:
                raise ValueError("Нельзя сформировать пустой рейс.")
            if trip.status not in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED}:
                raise ValueError("Этот рейс уже сформирован.")
            if _first_active_employee_by_roles("storekeeper") is None:
                raise ValueError("Не найден активный кладовщик для передачи сформированного рейса.")
            order_numbers = list(trip.orders.values_list("shipping_order__number", flat=True))
            packing_payloads = _packing_payload_map([n for n in order_numbers if n])
            from logistics.trip_validation import raise_if_trip_invalid

            raise_if_trip_invalid(trip, packing_payloads=packing_payloads)
            for link in trip.orders.select_related("shipping_order"):
                order = link.shipping_order
                pallet_error = _shipping_trip_pallet_error(
                    order,
                    packing_payloads.get(order.number) or {},
                )
                if pallet_error:
                    raise ValueError(
                        f"Невозможно утвердить рейс: заявка "
                        f"{format_order_number('shipping', order.number)} заблокирована: "
                        f"{pallet_error}."
                    )
            public_number = _assign_public_trip_number(trip)
            bump_trip_edit_version(
                trip,
                expected_version=expected_version,
                editor=editor,
                extra_updates={
                    "number": public_number,
                    "trip_date": trip.trip_date,
                    "vehicle_number": trip.vehicle_number,
                    "driver_name": trip.driver_name,
                    "driver_phone": trip.driver_phone,
                    "carrier": trip.carrier,
                    "max_weight_kg": trip.max_weight_kg,
                    "max_volume_m3": trip.max_volume_m3,
                    "max_pallets": trip.max_pallets,
                    "status": LogisticsTrip.STATUS_LOADING,
                },
            )
            try:
                from logistics.routing_services import sync_routing_from_trip

                for link in trip.orders.select_related("shipping_order"):
                    sync_routing_from_trip(link.shipping_order)
            except Exception:
                pass
            _close_trip_tasks_by_role(trip, "logistician")
            _ensure_trip_storekeeper_task(trip, request.user)
            try:
                from client_cabinet.chat_trips import ensure_trip_thread

                ensure_trip_thread(trip, user=request.user)
            except Exception:
                pass
        except TripEditConflict as exc:
            return _trip_mutation_response(request, trip=trip, conflict=exc)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("logistics:trip-detail", pk=trip.pk)
        messages.success(request, f"Рейс {_trip_public_number(trip)} сформирован и передан кладовщику.")
        return redirect("logistics:trip-detail", pk=trip.pk)
    if action == "update_sequences":
        try:
            bump_trip_edit_version(trip, expected_version=expected_version, editor=editor)
        except TripEditConflict as exc:
            return _trip_mutation_response(request, trip=trip, conflict=exc)
        ordered_ids = [_parse_int(value) for value in request.POST.getlist("ordered_trip_item_ids") if _parse_int(value) > 0]
        ordered_items = []
        if ordered_ids:
            item_map = {item.id: item for item in trip.orders.all()}
            ordered_items = [item_map[item_id] for item_id in ordered_ids if item_id in item_map]
        if not ordered_items:
            ordered_items = list(trip.orders.all())
        for index, item in enumerate(ordered_items, start=1):
            item.loading_sequence = index
            item.delivery_sequence = index
            item.comment = str(request.POST.get(f"comment_{item.id}") or "").strip()
            item.save(update_fields=["loading_sequence", "delivery_sequence", "comment", "updated_at"])
        return _trip_mutation_response(request, trip=trip, success_message="Порядок заявок в рейсе обновлен.")
    return None


def get_trip_loading_trip(pk: int) -> LogisticsTrip:
    return get_object_or_404(
        LogisticsTrip.objects.select_related("assigned_logistician", "created_by", "carrier"),
        pk=pk,
    )


def build_logistics_trip_loading_context(*, role: str, trip: LogisticsTrip) -> dict:
    rows = _trip_loading_rows(trip)
    progress = _trip_loading_progress(trip, rows)
    can_scan_loading = _can_start_loading(role, trip)
    return {
        "role": role,
        "cabinet_url": resolve_cabinet_url(role),
        "trip": trip,
        "trip_display_number": _trip_public_number(trip),
        "trip_status_label": _trip_status_label(trip),
        "trip_detail_url": f"/logistics/trips/{trip.pk}/",
        "can_scan_loading": can_scan_loading,
        "can_finish_loading": can_scan_loading and bool(progress["all_complete"]),
        "scanner_agents": _scanner_agents_payload(),
        "loading_rows": rows,
        "loading_progress": progress,
        "loading_state_payload": _trip_loading_response_payload(trip, rows, progress),
        "loading_document_rows": _trip_loading_document_rows(rows),
        "shipping_service_targets": _trip_loading_shipping_service_targets(rows),
    }


def handle_logistics_trip_loading_post(request, *, role: str, trip: LogisticsTrip):
    rows = _trip_loading_rows(trip)
    progress = _trip_loading_progress(trip, rows)
    can_scan_loading = _can_start_loading(role, trip)
    if not can_scan_loading:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
        return HttpResponseForbidden("Доступ запрещен")
    action = str(request.POST.get("action") or "").strip()
    if action == "finish_loading":
        if not progress["all_complete"]:
            messages.error(request, "Сначала загрузите все паллеты по рейсу.")
            return redirect("logistics:trip-loading", pk=trip.pk)
        try:
            from billing.warehouse_services import (
                require_completion_facts,
                sync_shipping_facts_to_billing,
            )

            for row in rows:
                order = row["order"]
                require_completion_facts(
                    client=order.agency,
                    order_type="shipping",
                    order_id=order.number,
                )
            _sync_trip_loading_completion_to_warehouse(trip, user=request.user)
            for row in rows:
                order = row["order"]
                sync_shipping_facts_to_billing(
                    client=order.agency,
                    order_id=order.number,
                    completed_at=timezone.now(),
                    user=request.user,
                    source_payload={
                        "trip_id": trip.pk,
                        "trip_number": trip.number,
                        "items_count": int(row.get("item_count") or 0),
                        "box_count": int(row.get("box_count") or 0),
                        "pallet_count": int(row.get("pallet_count") or 0),
                    },
                )
            with transaction.atomic():
                trip.status = LogisticsTrip.STATUS_DEPARTED
                trip.save(update_fields=["status", "updated_at"])
                _close_trip_tasks_by_role(trip, "storekeeper")
                _ensure_trip_delivery_task(trip, request.user)
            try:
                from logistics.routing_services import sync_routing_from_trip

                for link in trip.orders.select_related("shipping_order"):
                    sync_routing_from_trip(link.shipping_order)
            except Exception:
                pass
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect("logistics:trip-loading", pk=trip.pk)
        _launch_trip_shipping_reconcile(trip)
        return redirect("logistics:trip-detail", pk=trip.pk)
    scan_value = _normalize_loading_scan(request.POST.get("scan_value"))
    if not scan_value:
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {"ok": False, "status": "empty", "tone": "error", "message": "Отсканируй паллету перед подтверждением."},
            ),
            status=400,
        )
    current_row = progress["current_row"]
    if current_row is None:
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {"ok": False, "status": "complete", "tone": "success", "message": "Все паллеты по рейсу уже загружены."},
            )
        )
    if current_row.get("blocking_error"):
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {
                    "ok": False,
                    "status": "blocked",
                    "tone": "error",
                    "message": (
                        f"Заявка {current_row['display_number']} заблокирована: "
                        f"{current_row['blocking_error']}."
                    ),
                },
            ),
            status=409,
        )
    matched_pallet = next(
        (
            pallet
            for pallet in current_row["pallets"]
            if scan_value in pallet["accepted_scans"]
        ),
        None,
    )
    matched_row = current_row if matched_pallet is not None else None
    for row in rows if matched_pallet is None else []:
        for pallet in row["pallets"]:
            if scan_value in pallet["accepted_scans"]:
                matched_pallet = pallet
                matched_row = row
                break
        if matched_pallet:
            break
    if matched_pallet is None or matched_row is None:
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {"ok": False, "status": "unknown", "tone": "error", "message": "Паллета не найдена в этом рейсе."},
            ),
            status=400,
        )
    if matched_pallet["progress_key"] in progress["loaded_set"]:
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {
                    "ok": False,
                    "status": "duplicate",
                    "tone": "error",
                    "message": f"Паллета {matched_pallet['pallet_label']} уже загружена.",
                },
            ),
            status=400,
        )
    if matched_row["order"].pk != current_row["order"].pk:
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {
                    "ok": False,
                    "status": "wrong_order",
                    "tone": "error",
                    "message": (
                        f"Сейчас очередь заявки {current_row['display_number']}. "
                        f"Паллета {matched_pallet['pallet_label']} относится к {matched_row['display_number']}."
                    ),
                },
            ),
            status=409,
        )
    matching_pallet_count = sum(
        1
        for row in rows
        for pallet in row["pallets"]
        if matched_pallet["load_key_norm"] == pallet["load_key_norm"]
    )
    stored_key = (
        matched_pallet["progress_key"]
        if matching_pallet_count > 1
        else matched_pallet["load_key_norm"]
    )
    next_loaded_keys = progress["loaded_keys"] + [stored_key]
    matched_row_complete = all(
        pallet["progress_key"] == matched_pallet["progress_key"] or bool(pallet.get("is_loaded"))
        for pallet in matched_row["pallets"]
        if pallet["progress_key"]
    )
    try:
        with transaction.atomic():
            _save_trip_loading_progress(
                trip,
                loaded_keys=next_loaded_keys,
                pallet=matched_pallet,
                order=matched_row["order"],
                user=request.user,
            )
            if matched_row_complete:
                _record_order_loading_from_confirmed_scans(
                    trip,
                    order=matched_row["order"],
                    user=request.user,
                )
    except (ValueError, ValidationError) as exc:
        error_messages = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {
                    "ok": False,
                    "status": "blocked",
                    "tone": "error",
                    "message": "; ".join(error_messages),
                },
            ),
            status=409,
        )
    rows = _trip_loading_rows(trip)
    progress = _trip_loading_progress(trip, rows)
    next_row = progress["current_row"]
    message = f"Паллета {matched_pallet['pallet_label']} загружена."
    if next_row and next_row["order"].pk != matched_row["order"].pk:
        message = f"{message} Следующая очередь: {next_row['display_number']}."
    elif progress["all_complete"]:
        message = f"{message} Все паллеты по рейсу загружены."
    return JsonResponse(
        _trip_loading_response_payload(
            trip,
            rows,
            progress,
            {"ok": True, "status": "loaded", "tone": "success", "message": message},
        )
    )
