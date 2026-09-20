from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlencode

from django.db.models import Max, Q, Sum
from django.http import HttpResponseForbidden
from django.utils import timezone

from employees.access import get_request_role, is_staff_role
from fullbox.order_numbers import format_order_number
from labels.utils import load_label_settings
from sku.models import Agency, SKU
from sklad.models import WarehouseReserve, WarehouseStockSnapshot
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_transitions import WarehouseStateCode

_IP_PREFIX_RE = re.compile(r"\bиндивидуальный предприниматель\b", re.IGNORECASE)
_OS_LINE_DISPLAY_LABELS = {
    1: "0",
    2: "A",
    3: "B",
    4: "C",
    5: "D",
    6: "E",
    7: "F",
    8: "G",
    9: "I",
}
_JOURNAL_HIDDEN_WAREHOUSE_STATES = {
    WarehouseStateCode.IN_PROCESSING_ZONE.value,
    WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
}
_JOURNAL_COLUMN_FILTERS = {
    "f_when": "created_at",
    "f_pallet": "pallet_code",
    "f_box": "box_code",
    "f_order": "order_display",
    "f_client": "client_label",
    "f_sku": "sku",
    "f_name": "name",
    "f_size": "size",
    "f_weight": "box_weight",
    "f_goods_type": "goods_type",
    "f_location": "location_short",
}
_JOURNAL_SORT_DEFAULT_DIR = {
    "created_at": "desc",
    "pallet_code": "asc",
    "box_code": "asc",
    "order_display": "asc",
    "client_label": "asc",
    "sku": "asc",
    "name": "asc",
    "size": "asc",
    "box_weight": "asc",
    "goods_type": "asc",
    "location": "asc",
    "qty": "desc",
    "available_qty": "desc",
    "processing_reserved_qty": "desc",
    "processing_in_progress_qty": "desc",
    "shipping_reserved_qty": "desc",
}


def _shorten_ip_name(name: str) -> str:
    if not name:
        return "-"
    normalized = _IP_PREFIX_RE.sub("ИП", name)
    return " ".join(normalized.split())


def _location_zone_token(location: object | None) -> str:
    text = str(location or "").strip()
    if not text:
        return ""
    token = text.split("·", 1)[0].split("•", 1)[0].strip()
    return token or text


def _short_location_label(
    *,
    zone: object | None,
    row: object | None = None,
    section: object | None = None,
    tier: object | None = None,
    cell: object | None = None,
    location: object | None = None,
) -> str:
    zone_code = str(zone or "").strip().upper() or _location_zone_token(location).upper() or "-"
    row_no = _parse_qty_value(row) or 0
    section_no = _parse_qty_value(section) or 0
    tier_no = _parse_qty_value(tier) or 0
    cell_no = _parse_qty_value(cell) or 0
    location_text = str(location or "").strip()

    def _os_short_code() -> str:
        line_label = _OS_LINE_DISPLAY_LABELS.get(section_no, str(section_no or "").strip())
        if line_label and row_no and tier_no and cell_no:
            return f"{line_label}-{row_no}/{tier_no}-{cell_no}"
        if line_label and row_no:
            return f"{line_label}-{row_no}"
        return "OS"

    if zone_code == "PR":
        return "PR"
    if zone_code == "OBR":
        return "OBR"
    if zone_code == "OTG":
        return "OTG"
    if zone_code == "MR":
        return f"MR-{row_no}" if row_no else "MR"
    if zone_code == "OS":
        if row_no and section_no:
            return _os_short_code()
        if "·" in location_text:
            tail = location_text.split("·", 1)[1].strip()
            if tail:
                return tail
        return "OS"
    return zone_code or location_text or "-"


def _client_agency_for_request(request):
    if not request.user.is_authenticated:
        return None
    return Agency.objects.filter(portal_user=request.user).first()


def _parse_qty_value(raw: object | None) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _reserve_key(
    agency_id: int | None,
    sku: str | None,
    size: str | None,
    goods_type: str | None,
) -> tuple[int, str, str, str]:
    return (
        int(agency_id or 0),
        (sku or "").strip().lower(),
        (size or "").strip().lower(),
        StockAvailabilityService.normalize_goods_type(goods_type or ""),
    )


def _processing_snapshot_order_id(snapshot: WarehouseStockSnapshot) -> str:
    operation = getattr(snapshot, "active_operation", None)
    if operation and str(getattr(operation, "context_type", "") or "").strip() == "processing":
        order_id = str(getattr(operation, "context_id", "") or "").strip()
        if order_id:
            return order_id
    reserve = (
        WarehouseReserve.objects.filter(
            agency=snapshot.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
        )
        .exclude(
            status__in=[
                WarehouseReserve.STATUS_RELEASED,
                WarehouseReserve.STATUS_CANCELED,
            ]
        )
        .order_by("-updated_at", "-id")
        .first()
    )
    if reserve is None:
        return ""
    return str(reserve.context_id or "").strip()


def _processing_reserve_groups(agency: Agency | None) -> tuple[list[dict], dict[tuple[int, str, str, str], int]]:
    warehouse_qs = WarehouseReserve.objects.filter(
        reserve_type=WarehouseReserve.TYPE_PROCESSING,
    ).exclude(
        status__in=[
            WarehouseReserve.STATUS_RELEASED,
            WarehouseReserve.STATUS_CANCELED,
        ]
    )
    if agency:
        warehouse_qs = warehouse_qs.filter(agency=agency)
    warehouse_groups = list(
        warehouse_qs
        .values("agency_id", "context_id", "sku_code", "size", "goods_type")
        .annotate(
            reserved_qty=Sum("qty_reserved"),
            satisfied_qty=Sum("qty_satisfied"),
            updated_at_max=Max("updated_at"),
        )
    )
    in_progress_by_order_key: dict[tuple[int, str, tuple[int, str, str, str]], int] = {}
    in_progress_snapshots = (
        WarehouseStockSnapshot.objects.filter(
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
            processing_reserved_qty__gt=0,
            is_archived=False,
        )
        .select_related("agency", "active_operation")
        .order_by("id")
    )
    if agency:
        in_progress_snapshots = in_progress_snapshots.filter(agency=agency)
    for snapshot in in_progress_snapshots:
        order_id = _processing_snapshot_order_id(snapshot)
        if not order_id:
            continue
        order_key = (int(snapshot.agency_id or 0), order_id)
        reserve_key = _reserve_key(
            snapshot.agency_id,
            snapshot.sku_code,
            snapshot.size,
            snapshot.goods_type,
        )
        qty_value = int(snapshot.processing_reserved_qty or snapshot.qty or 0)
        if qty_value <= 0:
            continue
        combined_key = (order_key[0], order_key[1], reserve_key)
        in_progress_by_order_key[combined_key] = in_progress_by_order_key.get(combined_key, 0) + qty_value
    totals: dict[tuple[int, str, str, str], int] = {}
    normalized_groups: list[dict] = []
    for item in warehouse_groups:
        reserved_qty = int(item.get("reserved_qty") or 0)
        satisfied_qty = int(item.get("satisfied_qty") or 0)
        reserve_key = _reserve_key(
            item.get("agency_id"),
            item.get("sku_code"),
            item.get("size"),
            item.get("goods_type"),
        )
        order_key = (
            int(item.get("agency_id") or 0),
            str(item.get("context_id") or "").strip(),
            reserve_key,
        )
        waiting_qty = max(reserved_qty - satisfied_qty - int(in_progress_by_order_key.get(order_key, 0)), 0)
        if waiting_qty <= 0:
            continue
        normalized_item = {
            "agency_id": item.get("agency_id"),
            "sku": item.get("sku_code"),
            "size": item.get("size"),
            "goods_type": item.get("goods_type"),
            "reserved_qty": waiting_qty,
            "updated_at_max": item.get("updated_at_max"),
        }
        normalized_groups.append(normalized_item)
        totals[reserve_key] = totals.get(reserve_key, 0) + waiting_qty

    return normalized_groups, totals


def _shipping_reserve_groups(agency: Agency | None) -> tuple[list[dict], dict[tuple[int, str, str, str], int]]:
    qs = WarehouseReserve.objects.filter(reserve_type=WarehouseReserve.TYPE_SHIPPING).exclude(
        status__in=[
            WarehouseReserve.STATUS_RELEASED,
            WarehouseReserve.STATUS_CANCELED,
        ]
    )
    if agency:
        qs = qs.filter(agency=agency)
    groups = list(
        qs
        .values("agency_id", "sku_code", "size", "goods_type")
        .annotate(
            reserved_qty=Sum("qty_reserved"),
            satisfied_qty=Sum("qty_satisfied"),
            updated_at_max=Max("updated_at"),
        )
    )
    totals: dict[tuple[int, str, str, str], int] = {}
    normalized_groups: list[dict] = []
    for item in groups:
        qty = max(int(item.get("reserved_qty") or 0) - int(item.get("satisfied_qty") or 0), 0)
        if qty <= 0:
            continue
        normalized_item = {
            "agency_id": item.get("agency_id"),
            "sku_code": item.get("sku_code"),
            "size": item.get("size"),
            "goods_type": item.get("goods_type"),
            "reserved_qty": qty,
            "updated_at_max": item.get("updated_at_max"),
        }
        normalized_groups.append(normalized_item)
        key = _reserve_key(item.get("agency_id"), item.get("sku_code"), item.get("size"), item.get("goods_type"))
        totals[key] = totals.get(key, 0) + qty
    return normalized_groups, totals


def _processing_in_progress_groups(agency: Agency | None) -> tuple[list[dict], dict[tuple[int, str, str, str], int]]:
    grouped: dict[tuple[int, str, str, str], dict] = {}
    totals: dict[tuple[int, str, str, str], int] = {}
    warehouse_snapshots = (
        WarehouseStockSnapshot.objects.filter(
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
            is_archived=False,
        )
        .select_related("agency", "active_operation")
        .order_by("id")
    )
    if agency:
        warehouse_snapshots = warehouse_snapshots.filter(agency=agency)
    for snapshot in warehouse_snapshots:
        order_id = _processing_snapshot_order_id(snapshot)
        if not order_id:
            continue
        qty_value = int(snapshot.processing_reserved_qty or snapshot.qty or 0)
        if qty_value <= 0:
            continue
        key = _reserve_key(
            snapshot.agency_id,
            snapshot.sku_code,
            snapshot.size,
            snapshot.goods_type,
        )
        totals[key] = totals.get(key, 0) + qty_value
        row = grouped.get(key)
        if row is None:
            row = {
                "agency_id": int(snapshot.agency_id or 0),
                "sku": snapshot.sku_code,
                "size": snapshot.size or "-",
                "goods_type": snapshot.goods_type or "-",
                "in_progress_qty": 0,
                "updated_at_max": snapshot.updated_at,
            }
            grouped[key] = row
        row["in_progress_qty"] += qty_value
        if snapshot.updated_at and snapshot.updated_at > row["updated_at_max"]:
            row["updated_at_max"] = snapshot.updated_at
    return list(grouped.values()), totals


def _journal_unique_values(rows: list[dict], key: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = str(row.get(key) or "").strip()
        if not value or value == "-":
            continue
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def _journal_normalized_text(value: object | None) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def _journal_datetime_label(value) -> str:
    if not value:
        return ""
    dt_value = value
    if timezone.is_naive(dt_value):
        dt_value = timezone.make_aware(dt_value, timezone.get_current_timezone())
    return timezone.localtime(dt_value).strftime("%d.%m.%Y %H:%M")


def _journal_filter_text(row: dict, filter_name: str) -> str:
    if filter_name == "f_when":
        return _journal_datetime_label(row.get("created_at"))
    if filter_name == "f_order":
        return " ".join(
            part
            for part in [
                str(row.get("order_display") or "").strip(),
                str(row.get("order_id") or "").strip(),
                str(row.get("order_type_label") or "").strip(),
            ]
            if part and part != "-"
        )
    if filter_name == "f_size":
        return str(row.get("box_size") or "").strip()
    if filter_name == "f_location":
        return " ".join(
            part
            for part in [
                str(row.get("location_short") or "").strip(),
                str(row.get("location") or "").strip(),
                str(row.get("zone") or "").strip(),
            ]
            if part and part != "-"
        )
    field_name = _JOURNAL_COLUMN_FILTERS.get(filter_name)
    if not field_name:
        return ""
    return str(row.get(field_name) or "").strip()


def _journal_sort_value(row: dict, sort_key: str):
    if sort_key == "created_at":
        value = row.get("created_at")
        return value.timestamp() if value else 0
    if sort_key == "location":
        return (
            _journal_normalized_text(row.get("zone")),
            int(row.get("row_no") or 0),
            int(row.get("section_no") or 0),
            int(row.get("tier_no") or 0),
            int(row.get("cell_no") or 0),
            _journal_normalized_text(row.get("location_short")),
            _journal_normalized_text(row.get("location")),
        )
    if sort_key == "size":
        return _journal_normalized_text(row.get("box_size") or row.get("size"))
    if sort_key in {
        "qty",
        "box_weight",
        "available_qty",
        "processing_reserved_qty",
        "processing_in_progress_qty",
        "shipping_reserved_qty",
    }:
        return int(row.get(sort_key) or 0)
    return _journal_normalized_text(row.get(sort_key))


def _build_journal_query_url(request, **updates) -> str:
    params = request.GET.copy()
    for key, value in updates.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = str(value)
    query = params.urlencode()
    return f"{request.path}?{query}" if query else request.path


def _build_inventory_journal_summary(rows: list[dict]) -> dict:
    latest_activity = None
    distinct_skus: set[tuple[str, str, str]] = set()
    distinct_orders: set[str] = set()
    distinct_pallets: set[str] = set()
    distinct_boxes: set[str] = set()
    distinct_locations: set[str] = set()
    distinct_clients: set[str] = set()
    distinct_zones: set[str] = set()
    total_qty = 0
    total_available_qty = 0
    total_processing_reserved_qty = 0
    total_processing_in_progress_qty = 0
    total_shipping_reserved_qty = 0
    for row in rows:
        latest_value = row.get("created_at")
        if latest_value and (latest_activity is None or latest_value > latest_activity):
            latest_activity = latest_value
        distinct_skus.add(
            (
                str(row.get("sku") or "").strip(),
                str(row.get("size") or "").strip(),
                str(row.get("goods_type") or "").strip(),
            )
        )
        order_label = str(row.get("order_display") or "").strip() or format_order_number(
            row.get("order_type"),
            row.get("order_id"),
        )
        if order_label and order_label != "-":
            distinct_orders.add(order_label)
        pallet_code = str(row.get("pallet_code") or "").strip()
        if pallet_code and pallet_code != "-":
            distinct_pallets.add(pallet_code)
        box_code = str(row.get("box_code") or "").strip()
        if box_code and box_code != "-":
            distinct_boxes.add(box_code)
        location_value = str(row.get("location") or "").strip()
        if location_value and location_value != "-":
            distinct_locations.add(location_value)
        client_value = str(row.get("client_label") or "").strip()
        if client_value and client_value != "-":
            distinct_clients.add(client_value)
        zone_value = str(row.get("zone") or "").strip()
        if zone_value and zone_value != "-":
            distinct_zones.add(zone_value)
        total_qty += int(row.get("qty") or 0)
        total_available_qty += int(row.get("available_qty") or 0)
        total_processing_reserved_qty += int(row.get("processing_reserved_qty") or 0)
        total_processing_in_progress_qty += int(row.get("processing_in_progress_qty") or 0)
        total_shipping_reserved_qty += int(row.get("shipping_reserved_qty") or 0)
    return {
        "row_count": len(rows),
        "sku_count": len([item for item in distinct_skus if any(item)]),
        "order_count": len(distinct_orders),
        "pallet_count": len(distinct_pallets),
        "box_count": len(distinct_boxes),
        "location_count": len(distinct_locations),
        "client_count": len(distinct_clients),
        "zone_count": len(distinct_zones),
        "total_qty": total_qty,
        "total_available_qty": total_available_qty,
        "total_processing_reserved_qty": total_processing_reserved_qty,
        "total_processing_in_progress_qty": total_processing_in_progress_qty,
        "total_shipping_reserved_qty": total_shipping_reserved_qty,
        "latest_activity": latest_activity,
    }


def _build_inventory_journal_focus(rows: list[dict], q: str) -> dict | None:
    q_value = str(q or "").strip()
    if not q_value or not rows:
        return None
    q_lower = q_value.lower()
    pallets = _journal_unique_values(rows, "pallet_code")
    boxes = _journal_unique_values(rows, "box_code")
    locations = _journal_unique_values(rows, "location")
    clients = _journal_unique_values(rows, "client_label")
    zones = _journal_unique_values(rows, "zone")
    orders = [
        str(row.get("order_display") or "").strip()
        or format_order_number(row.get("order_type"), row.get("order_id"))
        for row in rows
        if str(row.get("order_id") or "").strip() not in {"", "-"}
    ]
    distinct_orders = list(dict.fromkeys(orders))
    sku_labels = list(
        dict.fromkeys(
            " · ".join(
                part
                for part in [
                    str(row.get("sku") or "").strip(),
                    str(row.get("name") or "").strip(),
                    str(row.get("size") or "").strip(),
                ]
                if part and part != "-"
            )
            for row in rows
        )
    )
    label = "Результат поиска"
    value = q_value
    if len(pallets) == 1 and q_lower in pallets[0].lower():
        label = "Паллета"
        value = pallets[0]
    elif len(boxes) == 1 and q_lower in boxes[0].lower():
        label = "Короб"
        value = boxes[0]
    elif len(locations) == 1 and q_lower in locations[0].lower():
        label = "Место"
        value = locations[0]
    elif len(sku_labels) == 1 and q_lower in sku_labels[0].lower():
        label = "SKU"
        value = sku_labels[0]
    summary = _build_inventory_journal_summary(rows)
    return {
        "label": label,
        "value": value,
        "query": q_value,
        "client_label": clients[0] if len(clients) == 1 else "",
        "zone_label": zones[0] if len(zones) == 1 else "",
        "location_label": locations[0] if len(locations) == 1 else "",
        "order_label": distinct_orders[0] if len(distinct_orders) == 1 else "",
        "sku_labels": sku_labels[:6],
        "summary": summary,
    }


def build_inventory_journal_page(*, request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    role = get_request_role(request)
    staff_view = request.user.is_staff or is_staff_role(role)
    client_agency = None
    if staff_view:
        client_id = request.GET.get("client") or request.GET.get("agency")
        if client_id:
            client_agency = Agency.objects.filter(pk=client_id).first()
    else:
        client_agency = _client_agency_for_request(request)
        if not client_agency:
            return HttpResponseForbidden("Доступ запрещен")
    rows = []
    order_type_codes = {
        "receiving": "ПРМ",
        "processing": "ОБР",
    }
    order_type_titles = {
        "receiving": "Приемка",
        "processing": "Обработка",
    }
    warehouse_rows = StockAvailabilityService.stock_rows_with_availability(agency=client_agency) if client_agency else StockAvailabilityService.stock_rows_with_availability()
    storage_rows = [
        item
        for item in warehouse_rows
        if str(item.get("warehouse_state_code") or "").strip() not in _JOURNAL_HIDDEN_WAREHOUSE_STATES
    ]
    processing_reserve_groups, processing_reserve_totals = _processing_reserve_groups(client_agency)
    processing_in_progress_groups, processing_in_progress_totals = _processing_in_progress_groups(client_agency)
    shipping_reserve_groups, shipping_reserve_totals = _shipping_reserve_groups(client_agency)
    for item in storage_rows:
        agency_obj = item.get("agency")
        client_name = _shorten_ip_name(
            (agency_obj.agn_name if agency_obj else "")
            or (agency_obj.fio_agn if agency_obj else "")
            or str(agency_obj or "")
        )
        rows.append(
            {
                "created_at": item.get("updated_at"),
                "order_id": item.get("order_id"),
                "order_type": item.get("order_type"),
                "order_type_code": order_type_codes.get(item.get("order_type"), (item.get("order_type") or "").upper()),
                "order_type_label": order_type_titles.get(item.get("order_type"), item.get("order_type")),
                "client_label": client_name or "-",
                "agency_id": int(item.get("agency_id") or 0),
                "sku": (item.get("sku") or "-").strip() or "-",
                "name": (item.get("name") or "-").strip() or "-",
                "size": (item.get("size") or "-").strip() or "-",
                "box_size": (item.get("box_size") or "").strip(),
                "box_weight": (item.get("box_weight") or "").strip(),
                "goods_type": (item.get("goods_type") or "-").strip() or "-",
                "qty": int(item.get("qty") or 0),
                "processing_reserved_qty": int(item.get("processing_reserved_qty") or 0),
                "processing_in_progress_qty": 0,
                "shipping_reserved_qty": int(item.get("shipping_reserved_qty") or 0),
                "available_qty": int(item.get("available_qty") or 0),
                "box_code": (item.get("box_code") or "-").strip() or "-",
                "pallet_code": (item.get("pallet_code") or "-").strip() or "-",
                "row_no": int(item.get("row") or 0),
                "section_no": int(item.get("section") or 0),
                "tier_no": int(item.get("tier") or 0),
                "cell_no": int(item.get("cell") or 0),
                "zone": (
                    item.get("zone")
                    or item.get("zone_code")
                    or _location_zone_token(item.get("location"))
                    or "-"
                ).strip() or "-",
                "location": (item.get("location") or item.get("zone") or "-").strip() or "-",
            }
        )

    if not staff_view:
        grouped = {}
        for row in rows:
            key = (row.get("sku"), row.get("name"), row.get("size"), row.get("goods_type"))
            qty_value = _parse_qty_value(row.get("qty")) or 0
            existing = grouped.get(key)
            if existing:
                existing["qty"] += qty_value
                existing["processing_reserved_qty"] += int(row.get("processing_reserved_qty") or 0)
                existing["processing_in_progress_qty"] += int(row.get("processing_in_progress_qty") or 0)
                existing["shipping_reserved_qty"] += int(row.get("shipping_reserved_qty") or 0)
                existing["available_qty"] += int(row.get("available_qty") or 0)
                if row.get("created_at") and row["created_at"] > existing.get("created_at"):
                    existing["created_at"] = row["created_at"]
            else:
                item = dict(row)
                item["qty"] = qty_value
                grouped[key] = item
        rows = list(grouped.values())
        for row in rows:
            key = _reserve_key(row.get("agency_id"), row.get("sku"), row.get("size"), row.get("goods_type"))
            row["processing_in_progress_qty"] = processing_in_progress_totals.get(key, 0)
        present_keys = {
            _reserve_key(row.get("agency_id"), row.get("sku"), row.get("size"), row.get("goods_type"))
            for row in rows
        }
        name_by_sku = {
            (row.get("sku") or "").strip().lower(): (row.get("name") or "-").strip() or "-"
            for row in rows
            if (row.get("sku") or "").strip()
        }
        missing_skus = {
            sku_value
            for item in list(processing_reserve_groups) + list(processing_in_progress_groups) + list(shipping_reserve_groups)
            for sku_value in [
                (item.get("sku") or item.get("sku_code") or "").strip()
            ]
            if sku_value and sku_value.lower() not in name_by_sku
        }
        if missing_skus:
            for sku_obj in SKU.objects.filter(
                agency=client_agency,
                deleted=False,
                sku_code__in=missing_skus,
            ):
                sku_key = (sku_obj.sku_code or "").strip().lower()
                if sku_key and sku_key not in name_by_sku:
                    name_by_sku[sku_key] = (sku_obj.name or "-").strip() or "-"
        client_name = _shorten_ip_name(
            (client_agency.agn_name if client_agency else "")
            or (client_agency.fio_agn if client_agency else "")
            or str(client_agency or "")
        )
        reserve_only_rows: dict[tuple[int, str, str, str], dict] = {}

        def _upsert_reserve_only_row(item: dict, *, source: str) -> None:
            sku_value = (item.get("sku") or item.get("sku_code") or "").strip()
            if not sku_value:
                return
            size_value = (item.get("size") or "").strip()
            goods_raw = (item.get("goods_type") or "").strip()
            key = _reserve_key(
                int(client_agency.id or 0) if client_agency else 0,
                sku_value,
                size_value,
                goods_raw,
            )
            qty_value = int(item.get("reserved_qty") or 0)
            if qty_value <= 0:
                return
            row = reserve_only_rows.get(key)
            if row is None:
                row = {
                    "created_at": item.get("updated_at_max") or timezone.localtime(),
                    "order_id": "-",
                    "order_type": "processing",
                    "order_type_code": order_type_codes.get("processing", "ОБР"),
                    "order_type_label": order_type_titles.get("processing", "Обработка"),
                    "client_label": client_name or "-",
                    "agency_id": int(client_agency.id or 0) if client_agency else 0,
                    "sku": sku_value or "-",
                    "name": name_by_sku.get(sku_value.lower(), "-"),
                    "size": size_value or "-",
                    "goods_type": goods_raw or "-",
                    "qty": 0,
                    "processing_reserved_qty": 0,
                    "processing_in_progress_qty": 0,
                    "shipping_reserved_qty": 0,
                    "available_qty": 0,
                    "box_code": "-",
                    "pallet_code": "-",
                    "row_no": 0,
                    "section_no": 0,
                    "tier_no": 0,
                    "cell_no": 0,
                    "zone": "-",
                    "location": "-",
                }
                reserve_only_rows[key] = row
            if item.get("updated_at_max") and item["updated_at_max"] > row["created_at"]:
                row["created_at"] = item["updated_at_max"]
            if goods_raw and row.get("goods_type") in {"", "-"}:
                row["goods_type"] = goods_raw
            if source == "processing":
                row["processing_reserved_qty"] += qty_value
            elif source == "processing_in_progress":
                row["processing_in_progress_qty"] += qty_value
            elif source == "shipping":
                row["shipping_reserved_qty"] += qty_value

        for item in processing_reserve_groups:
            _upsert_reserve_only_row(item, source="processing")
        for item in processing_in_progress_groups:
            _upsert_reserve_only_row(
                {
                    "agency_id": item.get("agency_id"),
                    "sku": item.get("sku"),
                    "size": item.get("size"),
                    "goods_type": item.get("goods_type"),
                    "reserved_qty": item.get("in_progress_qty"),
                    "updated_at_max": item.get("updated_at_max"),
                },
                source="processing_in_progress",
            )
        for item in shipping_reserve_groups:
            _upsert_reserve_only_row(item, source="shipping")

        for key, row in reserve_only_rows.items():
            if key in present_keys:
                continue
            present_keys.add(key)
            rows.append(row)
    for row in rows:
        row["order_display"] = format_order_number(row.get("order_type"), row.get("order_id"))
        row["source_label"] = row["order_display"] if row["order_display"] != "-" else (row.get("order_type_code") or "-")
        row["location_short"] = _short_location_label(
            zone=row.get("zone"),
            row=row.get("row_no"),
            section=row.get("section_no"),
            tier=row.get("tier_no"),
            cell=row.get("cell_no"),
            location=row.get("location"),
        )
    q = (request.GET.get("q") or "").strip()
    date_from_raw = (request.GET.get("date_from") or "").strip()
    date_to_raw = (request.GET.get("date_to") or "").strip()
    column_filters = {
        key: (request.GET.get(key) or "").strip()
        for key in _JOURNAL_COLUMN_FILTERS
    }
    sort_key = (request.GET.get("sort") or "created_at").strip()
    if sort_key not in _JOURNAL_SORT_DEFAULT_DIR:
        sort_key = "created_at"
    sort_dir = (request.GET.get("dir") or _JOURNAL_SORT_DEFAULT_DIR.get(sort_key, "asc")).strip().lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = _JOURNAL_SORT_DEFAULT_DIR.get(sort_key, "asc")
    date_from = None
    date_to = None
    try:
        if date_from_raw:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d").date()
    except ValueError:
        date_from = None
        date_from_raw = ""
    try:
        if date_to_raw:
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d").date()
    except ValueError:
        date_to = None
        date_to_raw = ""
    if date_from or date_to or q or any(column_filters.values()):
        q_lower = q.lower()
        filtered_rows = []
        for row in rows:
            created_at = row.get("created_at")
            if created_at:
                created_date = created_at.date()
                if date_from and created_date < date_from:
                    continue
                if date_to and created_date > date_to:
                    continue
            if q_lower:
                search_blob = " ".join(
                    str(value)
                    for value in (
                        row.get("client_label"),
                        row.get("order_id"),
                        row.get("order_display"),
                        row.get("source_label"),
                        row.get("order_type_label"),
                        row.get("sku"),
                        row.get("name"),
                        row.get("size"),
                        row.get("goods_type"),
                        row.get("qty"),
                        row.get("processing_reserved_qty"),
                        row.get("processing_in_progress_qty"),
                        row.get("shipping_reserved_qty"),
                        row.get("available_qty"),
                        row.get("box_code"),
                        row.get("pallet_code"),
                        row.get("zone"),
                        row.get("location_short"),
                        row.get("location"),
                    )
                    if value not in (None, "")
                ).lower()
                if q_lower not in search_blob:
                    continue
            matched_column_filters = True
            for filter_name, filter_value in column_filters.items():
                filter_text = _journal_normalized_text(filter_value)
                if not filter_text:
                    continue
                row_text = _journal_normalized_text(_journal_filter_text(row, filter_name))
                if filter_text not in row_text:
                    matched_column_filters = False
                    break
            if not matched_column_filters:
                continue
            filtered_rows.append(row)
        rows = filtered_rows
    rows.sort(
        key=lambda item: _journal_sort_value(item, sort_key),
        reverse=(sort_dir == "desc"),
    )
    journal_summary = _build_inventory_journal_summary(rows)
    journal_focus = _build_inventory_journal_focus(rows, q)
    journal_has_filters = bool(q or date_from_raw or date_to_raw or any(column_filters.values()))
    sort_urls = {}
    for key, default_dir in _JOURNAL_SORT_DEFAULT_DIR.items():
        next_dir = default_dir
        if sort_key == key:
            next_dir = "desc" if sort_dir == "asc" else "asc"
        sort_urls[key] = _build_journal_query_url(request, sort=key, dir=next_dir)
    reset_params = {}
    if request.GET.get("client"):
        reset_params["client"] = request.GET.get("client")
    if request.GET.get("agency"):
        reset_params["agency"] = request.GET.get("agency")
    journal_reset_url = f"{request.path}?{urlencode(reset_params)}" if reset_params else request.path
    role = get_request_role(request)
    if not staff_view:
        template_name = "client_cabinet/inventory_journal.html"
    elif role == "manager":
        template_name = "teammanager/inventory_journal.html"
    else:
        template_name = "sklad/inventory_journal.html"
    return {
        "template_name": template_name,
        "context": {
            "rows": rows,
            "client_agency": client_agency,
            "staff_view": staff_view,
            "q": q,
            "date_from": date_from_raw,
            "date_to": date_to_raw,
            "column_filters": column_filters,
            "journal_sort_key": sort_key,
            "journal_sort_dir": sort_dir,
            "journal_sort_urls": sort_urls,
            "journal_has_filters": journal_has_filters,
            "journal_reset_url": journal_reset_url,
            "journal_summary": journal_summary,
            "journal_focus": journal_focus,
            "label_settings": load_label_settings(),
        },
    }
