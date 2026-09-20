from __future__ import annotations

import json
import re
from datetime import datetime
from operator import itemgetter
from urllib.parse import urlencode

from django.contrib.postgres.aggregates import ArrayAgg
from django.core.paginator import Paginator
from django.db.models import Count, Max, Min, Q, Sum
from django.http import HttpResponseForbidden
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.access import get_request_role, is_staff_role
from fbs.models import FbsBox, FbsPallet, FbsStockBalance
from fbs.services.physical_locations import VIRTUAL_FBS_PLAN_PREFIX
from fullbox.order_numbers import format_order_number
from labels.utils import load_label_settings
from processing_app.stages import (
    PROCESSING_STAGE_DRAFT,
    processing_is_cancelled,
    processing_is_done,
    processing_stage_from_payload,
)
from shipping.models import ShippingOrder, ShippingOrderItem
from sku.models import Agency, SKU, SKUBarcode
from sklad.models import WarehouseLocation, WarehouseReserve, WarehouseStockSnapshot
from sklad.services.problem_boxes import (
    daily_problem_control_route,
    missing_box_rows,
    problem_box_rows,
    problem_box_snapshot_ids,
    problem_pallet_rows,
)
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_events import WarehouseEventType
from sklad.services.warehouse_stock_rows import snapshot_stock_rows
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
_MANAGER_CABINET_ROLES = {
    "manager",
    "logistician",
    "head_manager",
    "director",
    "admin",
    "developer",
}


def _journal_display_lines(value) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return ["-"]
    parts = [part.strip() for part in re.split(r"(?:\r?\n|\s{2,})+", text) if part.strip()]
    return parts or [text]


_JOURNAL_COLUMN_FILTERS = {
    "f_when": "created_at",
    "f_pallet": "pallet_code",
    "f_box": "box_code",
    "f_order": "order_display",
    "f_client": "client_label",
    "f_sku": "sku",
    "f_barcode": "barcode",
    "f_name": "name",
    "f_size": "box_size",
    "f_weight": "box_weight",
    "f_goods_type": "goods_type",
    "f_location": "location_short",
    "f_qty": "qty",
    "f_fbs": "fbs_qty",
    "f_status": "stock_status_label",
}
_JOURNAL_SORT_DEFAULT_DIR = {
    "created_at": "desc",
    "pallet_code": "asc",
    "box_code": "asc",
    "order_display": "asc",
    "client_label": "asc",
    "sku": "asc",
    "barcode": "asc",
    "name": "asc",
    "size": "asc",
    "box_weight": "asc",
    "goods_type": "asc",
    "location": "asc",
    "qty": "desc",
    "fbs_qty": "desc",
    "stock_main_qty": "desc",
    "stock_processing_qty": "desc",
    "stock_otg_qty": "desc",
    "available_qty": "desc",
    "processing_work_qty": "desc",
    "processing_reserved_qty": "desc",
    "processing_in_progress_qty": "desc",
    "shipping_reserved_qty": "desc",
}
_JOURNAL_TARGET_CONTEXT_TYPES = {"processing", "shipping"}
_JOURNAL_CONTEXT_CLEARING_EVENT_TYPES = {
    WarehouseEventType.PROCESSING_RESERVE_RELEASED.value,
    WarehouseEventType.SHIPPING_RESERVE_RELEASED.value,
    WarehouseEventType.WAREHOUSE_CONTEXT_CANCELED.value,
    WarehouseEventType.STOCK_RETURNED_TO_STORAGE.value,
}
_JOURNAL_CANCELED_OPERATION_STATUSES = {"canceled"}
_JOURNAL_PROCESSING_CONSUMED_STATE = WarehouseStateCode.PROCESSING_CONSUMED.value
_JOURNAL_EDIT_RESERVED_STATES = {
    WarehouseStateCode.RESERVED_FOR_PROCESSING.value,
    WarehouseStateCode.RESERVED_FOR_SHIPPING.value,
}
_JOURNAL_EDIT_SHIPPING_STATES = {
    WarehouseStateCode.MOVING_TO_OTG.value,
    WarehouseStateCode.IN_OTG.value,
}
_JOURNAL_EDIT_PROCESSING_STATES = {
    WarehouseStateCode.MOVING_TO_PROCESSING.value,
    WarehouseStateCode.IN_PROCESSING_ZONE.value,
    WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
    WarehouseStateCode.PROCESSING_CONSUMED.value,
}
_JOURNAL_PROCESSING_SOURCE_BOX_STATES = {
    WarehouseStateCode.MOVING_TO_PROCESSING.value,
    WarehouseStateCode.IN_PROCESSING_ZONE.value,
    WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
}
_JOURNAL_OTG_STOCK_STATES = {
    WarehouseStateCode.MOVING_TO_OTG.value,
    WarehouseStateCode.IN_OTG.value,
    WarehouseStateCode.PALLETIZING.value,
    WarehouseStateCode.READY_FOR_LOADING.value,
    WarehouseStateCode.ASSIGNED_TO_TRIP.value,
    WarehouseStateCode.LOADING_IN_PROGRESS.value,
}
_JOURNAL_CLIENT_SHIPPING_STATES = _JOURNAL_OTG_STOCK_STATES | {
    WarehouseStateCode.LOADED_TO_VEHICLE.value,
}
_CLIENT_ACTIVE_SHIPPING_STATUSES = {
    ShippingOrder.STATUS_SUBMITTED,
    ShippingOrder.STATUS_RESERVED,
    ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
    ShippingOrder.STATUS_PICKING,
    ShippingOrder.STATUS_PACKED,
    ShippingOrder.STATUS_PARTIAL,
}
_JOURNAL_STOCK_METRIC_FIELDS = (
    "qty",
    "stock_main_qty",
    "stock_processing_qty",
    "stock_otg_qty",
    "available_qty",
    "processing_reserved_qty",
    "processing_in_progress_qty",
    "shipping_reserved_qty",
    "fbs_qty",
    "fbs_available_qty",
    "fbs_reserved_qty",
)
_JOURNAL_STATUS_FIELDS = (
    "stock_main_qty",
    "stock_processing_qty",
    "stock_otg_qty",
    "available_qty",
    "processing_reserved_qty",
    "processing_in_progress_qty",
    "shipping_reserved_qty",
    "fbs_available_qty",
    "fbs_reserved_qty",
)
# Browser datalists become expensive long before thousands of suggestions are
# useful. Filtering itself remains unrestricted; this only caps autocomplete.
_JOURNAL_FILTER_OPTION_LIMIT = 500
_JOURNAL_SOURCE_IDENTITY_GETTER = itemgetter(
    "source_order_id",
    "source_order_type",
    "order_id",
    "order_type",
    "container_id",
    "parent_container_id",
    "location_id",
    "active_context_type",
    "active_context_id",
    "active_context_status",
    "last_event_type",
    "last_event_context_type",
    "last_event_context_id",
    "warehouse_state_code",
    "agency_id",
    "sku",
    "barcode",
    "name",
    "size",
    "box_size",
    "box_weight",
    "goods_type",
    "is_fbs_stock",
    "box_code",
    "pallet_code",
    "row",
    "section",
    "tier",
    "cell",
    "zone",
    "location",
)
_SKU_STOCK_BOX_COUNT_RE = re.compile(r"коробов:\s*(\d+)", re.IGNORECASE)
_SKU_STOCK_BOX_QTY_RE = re.compile(r"кратность:\s*(\d+)", re.IGNORECASE)
_SKU_STOCK_ACTIVE_RESERVE_STATUSES = (
    WarehouseReserve.STATUS_ACTIVE,
    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
    WarehouseReserve.STATUS_ALLOCATED,
    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
)
_SKU_STOCK_COLUMN_FILTERS = {
    "f_client": "client_label",
    "f_sku": "sku",
    "f_name": "name",
    "f_size": "size",
    "f_barcode": "barcode",
    "f_goods_type": "goods_type",
    "f_quant": "box_quant_text",
}
_SKU_STOCK_SORT_FIELDS = {
    "client": "client_label",
    "sku": "sku",
    "name": "name",
    "size": "size",
    "barcode": "barcode",
    "goods_type": "goods_type",
    "physical_qty": "physical_qty",
    "main_qty": "main_qty",
    "processing_zone_qty": "processing_zone_qty",
    "otg_zone_qty": "otg_zone_qty",
    "shipping_reserved_qty": "shipping_reserved_qty",
    "box_quant": "box_quant_text",
    "processing_reserved_qty": "processing_reserved_qty",
    "available_to_promise_qty": "available_to_promise_qty",
    "latest_at": "latest_at",
}
_SKU_STOCK_SORT_DEFAULT_DIR = {
    "client": "asc",
    "sku": "asc",
    "name": "asc",
    "size": "asc",
    "barcode": "asc",
    "goods_type": "asc",
    "physical_qty": "desc",
    "main_qty": "desc",
    "processing_zone_qty": "desc",
    "otg_zone_qty": "desc",
    "shipping_reserved_qty": "desc",
    "box_quant": "asc",
    "processing_reserved_qty": "desc",
    "available_to_promise_qty": "desc",
    "latest_at": "desc",
}
_SKU_STOCK_NUMERIC_SORT_FIELDS = {
    "physical_qty",
    "main_qty",
    "processing_zone_qty",
    "otg_zone_qty",
    "shipping_reserved_qty",
    "processing_reserved_qty",
    "available_to_promise_qty",
}


def _journal_stock_bucket_for_row(row: dict) -> str:
    zone_code = str(row.get("zone") or row.get("zone_code") or "").strip().upper()
    state_code = str(row.get("warehouse_state_code") or "").strip().lower()
    if state_code == WarehouseStateCode.PROCESSING_CONSUMED.value:
        return ""
    if zone_code == "OTG" or state_code in _JOURNAL_OTG_STOCK_STATES:
        return "OTG"
    if zone_code == "OBR" or state_code in _JOURNAL_PROCESSING_SOURCE_BOX_STATES:
        return "OBR"
    if zone_code == "OS":
        return "OS"
    return zone_code


def _journal_container_search(value: str) -> str:
    search_value = str(value or "").strip()
    if not search_value:
        return ""
    if re.fullmatch(r"(?:TDT|OSN|KEZ|AFR)-[A-Za-z0-9-]+", search_value, flags=re.IGNORECASE):
        return search_value
    return ""


def _staff_inventory_snapshot_rows(
    *,
    agency: Agency | None = None,
    box_filter: str = "",
    pallet_filter: str = "",
    container_search: str = "",
    aggregate_for_journal: bool = False,
) -> list[dict]:
    snapshot_qs = WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0)
    fbs_qs = FbsStockBalance.objects.filter(
        qty__gt=0,
        box__status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
        box__pallet__status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
        box__pallet__cell__is_active=True,
    )
    if agency is not None:
        snapshot_qs = snapshot_qs.filter(agency=agency)
        fbs_qs = fbs_qs.filter(agency=agency)

    for value in (box_filter, pallet_filter, container_search):
        filter_value = str(value or "").strip()
        if not filter_value:
            continue
        snapshot_qs = snapshot_qs.filter(
            Q(container_code__icontains=filter_value)
            | Q(container__container_code__icontains=filter_value)
            | Q(parent_container__container_code__icontains=filter_value)
        )
        fbs_qs = fbs_qs.filter(
            Q(box__box_code__icontains=filter_value)
            | Q(box__pallet__pallet_code__icontains=filter_value)
            | Q(box__source_container__container_code__icontains=filter_value)
        )

    agency_ids = set(
        snapshot_qs.order_by().values_list("agency_id", flat=True).distinct()
    )
    agency_ids.update(
        fbs_qs.order_by().values_list("agency_id", flat=True).distinct()
    )
    agencies_by_id = Agency.objects.in_bulk(agency_ids)
    snapshot_value_fields = [
            "id",
            "agency_id",
            "container_id",
            "parent_container_id",
            "location_id",
            "source_context_type",
            "source_context_id",
            "sku_ref_id",
            "sku_code",
            "name",
            "size",
            "barcode",
            "goods_type",
            "warehouse_state_code",
            "qty",
            "available_qty",
            "processing_reserved_qty",
            "shipping_reserved_qty",
            "container_code",
            "zone_code",
            "updated_at",
            "sku_ref__sku_code",
            "container__container_type",
            "container__container_code",
            "container__width_mm",
            "container__height_mm",
            "container__depth_mm",
            "container__gross_weight_g",
            "parent_container__container_type",
            "parent_container__container_code",
            "location__zone_code",
            "location__row_no",
            "location__section_no",
            "location__tier_no",
            "location__cell_no",
            "location__display_name",
            "location__location_code",
            "active_operation__context_type",
            "active_operation__context_id",
            "active_operation__status",
            "last_event__event_type",
            "last_event__stock_context_type",
            "last_event__stock_context_id",
        ]
    if aggregate_for_journal:
        snapshot_group_fields = [
            field_name
            for field_name in snapshot_value_fields
            if field_name not in {
                "id",
                "sku_ref_id",
                "qty",
                "available_qty",
                "processing_reserved_qty",
                "shipping_reserved_qty",
                "updated_at",
            }
        ]
        snapshot_group_fields.append("last_event__payload__source_box_code")
        snapshots = list(
            snapshot_qs.order_by()
            .values(*snapshot_group_fields)
            .annotate(
                _journal_ids=ArrayAgg("id", ordering=("created_at", "id")),
                _journal_qty_values=ArrayAgg("qty", ordering=("created_at", "id")),
                _journal_source_row_count=Count("id"),
                _journal_first_created_at=Min("created_at"),
                _journal_updated_at=Max("updated_at"),
                _journal_qty_sum=Sum("qty"),
                _journal_available_qty_sum=Sum("available_qty"),
                _journal_processing_reserved_qty_sum=Sum("processing_reserved_qty"),
                _journal_shipping_reserved_qty_sum=Sum("shipping_reserved_qty"),
            )
        )
        for snapshot in snapshots:
            snapshot_ids = snapshot.pop("_journal_ids") or []
            qty_values = snapshot.pop("_journal_qty_values") or []
            snapshot["id"] = int(snapshot_ids[0] if snapshot_ids else 0)
            snapshot["sku_ref_id"] = 0
            snapshot["created_at"] = snapshot.pop("_journal_first_created_at", None)
            snapshot["_journal_first_qty"] = int(qty_values[0] if qty_values else 0)
            snapshot["updated_at"] = snapshot.pop("_journal_updated_at", None)
            snapshot["qty"] = snapshot.pop("_journal_qty_sum", 0)
            snapshot["available_qty"] = snapshot.pop("_journal_available_qty_sum", 0)
            snapshot["processing_reserved_qty"] = snapshot.pop(
                "_journal_processing_reserved_qty_sum",
                0,
            )
            snapshot["shipping_reserved_qty"] = snapshot.pop(
                "_journal_shipping_reserved_qty_sum",
                0,
            )
        snapshots.sort(
            key=lambda item: (
                item.get("created_at") is None,
                item.get("created_at") or timezone.now(),
                int(item.get("id") or 0),
            )
        )
    else:
        snapshot_value_fields.extend(("created_at", "last_event__payload"))
        snapshots = snapshot_qs.order_by("created_at", "id").values(
            *snapshot_value_fields
        ).iterator(chunk_size=2000)
    prepared_rows = []
    grouped_prepared_rows: dict[tuple, dict] = {}

    def append_prepared_row(prepared: dict) -> None:
        if not aggregate_for_journal:
            prepared_rows.append(prepared)
            return

        identity = _JOURNAL_SOURCE_IDENTITY_GETTER(prepared)
        existing = grouped_prepared_rows.get(identity)
        if existing is None:
            prepared["_journal_source_row_count"] = max(
                int(prepared.get("_journal_source_row_count") or 1),
                1,
            )
            prepared["_journal_first_qty"] = int(
                prepared.get("_journal_first_qty") or prepared.get("qty") or 0
            )
            grouped_prepared_rows[identity] = prepared
            return

        existing["_journal_source_row_count"] = int(
            existing.get("_journal_source_row_count") or 1
        ) + max(int(prepared.get("_journal_source_row_count") or 1), 1)
        for field_name in (
            "qty",
            "stock_main_qty",
            "stock_processing_qty",
            "stock_otg_qty",
            "available_qty",
            "processing_reserved_qty",
            "shipping_reserved_qty",
            "fbs_qty",
            "fbs_available_qty",
            "fbs_reserved_qty",
        ):
            existing[field_name] = int(existing.get(field_name) or 0) + int(
                prepared.get(field_name) or 0
            )
        if prepared.get("updated_at") and (
            not existing.get("updated_at")
            or prepared["updated_at"] > existing["updated_at"]
        ):
            existing["updated_at"] = prepared["updated_at"]
    for snapshot in snapshots:
        container_type = str(snapshot.get("container__container_type") or "").strip()
        container_code = str(snapshot.get("container__container_code") or "").strip()
        parent_container_type = str(snapshot.get("parent_container__container_type") or "").strip()
        parent_container_code = str(snapshot.get("parent_container__container_code") or "").strip()
        box_code = ""
        pallet_code = ""
        if container_type == "box":
            box_code = container_code
            if parent_container_type in {"pallet", "mixed_pallet"}:
                pallet_code = parent_container_code
        elif parent_container_code:
            pallet_code = parent_container_code
            box_code = container_code
        elif container_type in {"pallet", "mixed_pallet"}:
            pallet_code = container_code
        else:
            pallet_code = str(snapshot.get("container_code") or "").strip()

        state_code = str(snapshot.get("warehouse_state_code") or "").strip().lower()
        if state_code in _JOURNAL_PROCESSING_SOURCE_BOX_STATES:
            source_box_code = snapshot.get("last_event__payload__source_box_code")
            event_payload = (
                {"source_box_code": source_box_code}
                if aggregate_for_journal and source_box_code
                else snapshot.get("last_event__payload")
            )
            if isinstance(event_payload, dict) and not box_code:
                box_code = str(event_payload.get("source_box_code") or "").strip()
            pallet_code = ""

        zone = str(snapshot.get("location__zone_code") or snapshot.get("zone_code") or "").strip().upper()
        row_number = int(snapshot.get("location__row_no") or 0)
        section_number = int(snapshot.get("location__section_no") or 0)
        tier_number = int(snapshot.get("location__tier_no") or 0)
        cell_number = int(snapshot.get("location__cell_no") or 0)
        location = (
            str(snapshot.get("location__display_name") or "").strip()
            or str(snapshot.get("location__location_code") or "").strip()
            or zone
        )
        dimensions = (
            snapshot.get("container__width_mm"),
            snapshot.get("container__height_mm"),
            snapshot.get("container__depth_mm"),
        )
        prepared = {
            "id": int(snapshot.get("id") or 0),
            "agency": agencies_by_id.get(int(snapshot.get("agency_id") or 0)),
            "agency_id": int(snapshot.get("agency_id") or 0),
            "container_id": int(snapshot.get("container_id") or 0),
            "parent_container_id": int(snapshot.get("parent_container_id") or 0),
            "location_id": int(snapshot.get("location_id") or 0),
            "source_order_type": str(snapshot.get("source_context_type") or "").strip(),
            "source_order_id": str(snapshot.get("source_context_id") or "").strip(),
            "order_type": str(snapshot.get("source_context_type") or "").strip(),
            "order_id": str(snapshot.get("source_context_id") or "").strip(),
            "active_context_type": str(snapshot.get("active_operation__context_type") or "").strip(),
            "active_context_id": str(snapshot.get("active_operation__context_id") or "").strip(),
            "active_context_status": str(snapshot.get("active_operation__status") or "").strip(),
            "last_event_type": str(snapshot.get("last_event__event_type") or "").strip(),
            "last_event_context_type": str(snapshot.get("last_event__stock_context_type") or "").strip(),
            "last_event_context_id": str(snapshot.get("last_event__stock_context_id") or "").strip(),
            "sku": str(snapshot.get("sku_ref__sku_code") or snapshot.get("sku_code") or "").strip(),
            "sku_ref_id": int(snapshot.get("sku_ref_id") or 0),
            "name": str(snapshot.get("name") or "").strip(),
            "size": str(snapshot.get("size") or "").strip(),
            "box_size": "/".join(str(value or "-") for value in dimensions) if any(dimensions) else "",
            "box_weight": str(snapshot.get("container__gross_weight_g") or "").strip(),
            "barcode": str(snapshot.get("barcode") or "").strip(),
            "goods_type": str(snapshot.get("goods_type") or "").strip(),
            "warehouse_state_code": str(snapshot.get("warehouse_state_code") or "").strip(),
            "is_fbs_stock": False,
            "qty": int(snapshot.get("qty") or 0),
            "available_qty": int(snapshot.get("available_qty") or 0),
            "processing_reserved_qty": int(snapshot.get("processing_reserved_qty") or 0),
            "shipping_reserved_qty": int(snapshot.get("shipping_reserved_qty") or 0),
            "fbs_qty": 0,
            "fbs_available_qty": 0,
            "fbs_reserved_qty": 0,
            "pallet_code": pallet_code,
            "box_code": box_code,
            "zone": zone,
            "row": row_number,
            "section": section_number,
            "tier": tier_number,
            "cell": cell_number,
            "location": location,
            "created_at": snapshot.get("created_at"),
            "updated_at": snapshot.get("updated_at"),
        }
        stock_bucket = _journal_stock_bucket_for_row(prepared)
        qty_value = int(prepared.get("qty") or 0)
        prepared["stock_main_qty"] = qty_value if stock_bucket == "OS" else 0
        prepared["stock_processing_qty"] = qty_value if stock_bucket == "OBR" else 0
        prepared["stock_otg_qty"] = qty_value if stock_bucket == "OTG" else 0
        if aggregate_for_journal:
            prepared["_journal_source_row_count"] = int(
                snapshot.get("_journal_source_row_count") or 1
            )
            prepared["_journal_first_qty"] = int(
                snapshot.get("_journal_first_qty") or qty_value
            )
        append_prepared_row(prepared)

    fbs_value_fields = [
        "id",
        "agency_id",
        "sku_ref_id",
        "sku_code",
        "name",
        "size",
        "barcode",
        "goods_type",
        "qty",
        "available_qty",
        "reserved_qty",
        "updated_at",
        "box_id",
        "box__box_code",
        "box__width_mm",
        "box__height_mm",
        "box__depth_mm",
        "box__source_container_id",
        "box__source_container__gross_weight_g",
        "box__source_container__current_location_id",
        "box__source_container__current_location__zone_code",
        "box__source_container__current_location__zone_kind",
        "box__source_container__current_location__row_no",
        "box__source_container__current_location__section_no",
        "box__source_container__current_location__tier_no",
        "box__source_container__current_location__cell_no",
        "box__source_container__current_location__display_name",
        "box__source_container__current_location__location_code",
        "box__pallet_id",
        "box__pallet__pallet_code",
        "box__pallet__warehouse_container_id",
        "box__pallet__cell__cell_code",
        "box__pallet__cell__location_id",
        "box__pallet__cell__location__row_no",
        "box__pallet__cell__location__section_no",
        "box__pallet__cell__location__tier_no",
        "box__pallet__cell__location__cell_no",
        "box__pallet__cell__location__display_name",
        "box__pallet__cell__location__location_code",
    ]
    if aggregate_for_journal:
        fbs_group_fields = [
            field_name
            for field_name in fbs_value_fields
            if field_name not in {
                "id",
                "sku_ref_id",
                "qty",
                "available_qty",
                "reserved_qty",
                "updated_at",
            }
        ]
        fbs_balances = list(
            fbs_qs.order_by()
            .values(*fbs_group_fields)
            .annotate(
                _journal_ids=ArrayAgg("id", ordering=("created_at", "id")),
                _journal_qty_values=ArrayAgg("qty", ordering=("created_at", "id")),
                _journal_source_row_count=Count("id"),
                _journal_first_created_at=Min("created_at"),
                _journal_updated_at=Max("updated_at"),
                _journal_qty_sum=Sum("qty"),
                _journal_available_qty_sum=Sum("available_qty"),
                _journal_reserved_qty_sum=Sum("reserved_qty"),
            )
        )
        for balance in fbs_balances:
            balance_ids = balance.pop("_journal_ids") or []
            qty_values = balance.pop("_journal_qty_values") or []
            balance["id"] = int(balance_ids[0] if balance_ids else 0)
            balance["sku_ref_id"] = 0
            balance["created_at"] = balance.pop("_journal_first_created_at", None)
            balance["_journal_first_qty"] = int(qty_values[0] if qty_values else 0)
            balance["updated_at"] = balance.pop("_journal_updated_at", None)
            balance["qty"] = balance.pop("_journal_qty_sum", 0)
            balance["available_qty"] = balance.pop("_journal_available_qty_sum", 0)
            balance["reserved_qty"] = balance.pop("_journal_reserved_qty_sum", 0)
        fbs_balances.sort(
            key=lambda item: (
                item.get("created_at") is None,
                item.get("created_at") or timezone.now(),
                int(item.get("id") or 0),
            )
        )
    else:
        fbs_value_fields.append("created_at")
        fbs_balances = fbs_qs.order_by("created_at", "id").values(
            *fbs_value_fields
        ).iterator(chunk_size=2000)
    for balance in fbs_balances:
        dimensions = (
            balance.get("box__width_mm"),
            balance.get("box__height_mm"),
            balance.get("box__depth_mm"),
        )
        source_location_prefix = "box__source_container__current_location"
        source_location_code = str(
            balance.get(f"{source_location_prefix}__location_code") or ""
        ).strip()
        source_location_kind = str(
            balance.get(f"{source_location_prefix}__zone_kind") or ""
        ).strip().lower()
        source_location_is_physical = bool(
            balance.get("box__source_container__current_location_id")
            and source_location_kind != WarehouseLocation.ZONE_KIND_VIRTUAL
            and not source_location_code.upper().startswith(VIRTUAL_FBS_PLAN_PREFIX)
        )
        location_prefix = (
            source_location_prefix
            if source_location_is_physical
            else "box__pallet__cell__location"
        )
        fbs_cell_code = str(balance.get("box__pallet__cell__cell_code") or "").strip()
        physical_location_code = str(
            balance.get(f"{location_prefix}__location_code") or ""
        ).strip()
        physical_location_label = str(
            balance.get(f"{location_prefix}__display_name") or ""
        ).strip()
        location_values = [
            "FBS",
            physical_location_code or (fbs_cell_code if not source_location_is_physical else ""),
            physical_location_label,
        ]
        location_parts = []
        seen_location_parts = set()
        for value in location_values:
            normalized_value = value.casefold()
            if not value or normalized_value in seen_location_parts:
                continue
            seen_location_parts.add(normalized_value)
            location_parts.append(value)
        location = " · ".join(location_parts)
        qty_value = int(balance.get("qty") or 0)
        prepared_balance = {
                "id": 0,
                "agency": agencies_by_id.get(int(balance.get("agency_id") or 0)),
                "agency_id": int(balance.get("agency_id") or 0),
                "container_id": int(balance.get("box__source_container_id") or 0),
                "parent_container_id": int(balance.get("box__pallet__warehouse_container_id") or 0),
                "location_id": int(balance.get(f"{location_prefix}_id") or 0),
                "source_order_type": "fbs",
                "source_order_id": "FBS",
                "order_type": "fbs",
                "order_id": "FBS",
                "active_context_type": "",
                "active_context_id": "",
                "active_context_status": "",
                "last_event_type": "",
                "last_event_context_type": "",
                "last_event_context_id": "",
                "sku": str(balance.get("sku_code") or "").strip(),
                "sku_ref_id": int(balance.get("sku_ref_id") or 0),
                "name": str(balance.get("name") or "").strip(),
                "size": str(balance.get("size") or "").strip(),
                "box_size": "/".join(str(value or "-") for value in dimensions) if any(dimensions) else "",
                "box_weight": str(balance.get("box__source_container__gross_weight_g") or "").strip(),
                "barcode": str(balance.get("barcode") or "").strip(),
                "goods_type": str(balance.get("goods_type") or "").strip(),
                "warehouse_state_code": "fbs_stock",
                "is_fbs_stock": True,
                "qty": qty_value,
                "available_qty": 0,
                "processing_reserved_qty": 0,
                "shipping_reserved_qty": 0,
                "fbs_qty": qty_value,
                "fbs_available_qty": int(balance.get("available_qty") or 0),
                "fbs_reserved_qty": int(balance.get("reserved_qty") or 0),
                "stock_main_qty": 0,
                "stock_processing_qty": 0,
                "stock_otg_qty": 0,
                "pallet_code": str(balance.get("box__pallet__pallet_code") or "").strip(),
                "box_code": str(balance.get("box__box_code") or "").strip(),
                "zone": "FBS",
                "row": int(balance.get(f"{location_prefix}__row_no") or 0),
                "section": int(balance.get(f"{location_prefix}__section_no") or 0),
                "tier": int(balance.get(f"{location_prefix}__tier_no") or 0),
                "cell": int(balance.get(f"{location_prefix}__cell_no") or 0),
                "location": location or "FBS",
                "created_at": balance.get("created_at"),
                "updated_at": balance.get("updated_at"),
            }
        if aggregate_for_journal:
            prepared_balance["_journal_source_row_count"] = int(
                balance.get("_journal_source_row_count") or 1
            )
            prepared_balance["_journal_first_qty"] = int(
                balance.get("_journal_first_qty") or qty_value
            )
        append_prepared_row(prepared_balance)
    if aggregate_for_journal:
        return list(grouped_prepared_rows.values())
    return prepared_rows


def _sku_stock_group_key(
    agency_id: int | None,
    sku: str | None,
    size: str | None,
    barcode: str | None,
    goods_type: str | None,
) -> tuple[int, str, str, str, str]:
    return (
        int(agency_id or 0),
        str(sku or "").strip().lower(),
        str(size or "").strip().lower(),
        str(barcode or "").strip().lower(),
        StockAvailabilityService.normalize_goods_type(goods_type or ""),
    )


def _sku_stock_row_key(
    base_key: tuple[int, str, str, str, str],
    *,
    box_quant_qty: int = 0,
    is_mixed_box: bool = False,
    quant_kind: str = "box",
) -> tuple[int, str, str, str, str, int, bool, str]:
    return (
        *base_key,
        max(int(box_quant_qty or 0), 0),
        bool(is_mixed_box),
        str(quant_kind or "box").strip().lower() or "box",
    )


def _sku_stock_label(value: object | None) -> str:
    text = str(value or "").strip()
    return text or "-"


def _sku_stock_client_prefix(agency: Agency | None) -> str:
    if not agency:
        return "-"
    return str(getattr(agency, "pref", "") or "").strip() or "-"


def _sku_stock_blank_row(
    *,
    agency: Agency | None,
    agency_id: int,
    sku: str,
    name: str,
    size: str,
    barcode: str,
    goods_type: str,
    box_quant_qty: int = 0,
    is_mixed_box: bool = False,
    quant_kind: str = "box",
) -> dict:
    normalized_quant_kind = str(quant_kind or "box").strip().lower() or "box"
    return {
        "agency_id": int(agency_id or 0),
        "client_label": _sku_stock_client_prefix(agency),
        "client_full_label": _agency_journal_label(agency),
        "sku": _sku_stock_label(sku),
        "name": _sku_stock_label(name),
        "size": _sku_stock_label(size),
        "barcode": _sku_stock_label(barcode),
        "goods_type": _sku_stock_label(goods_type),
        "box_quant_qty": max(int(box_quant_qty or 0), 0),
        "is_mixed_box": bool(is_mixed_box),
        "mixed_block_code": "",
        "mixed_block_label": "",
        "mixed_block_color_index": 0,
        "quant_kind": normalized_quant_kind,
        "row_css_class": "mixed-quant" if is_mixed_box else "",
        "physical_qty": 0,
        "main_qty": 0,
        "processing_zone_qty": 0,
        "otg_zone_qty": 0,
        "other_zone_qty": 0,
        "physical_available_qty": 0,
        "processing_reserved_qty": 0,
        "processing_snapshot_reserved_qty": 0,
        "processing_business_reserved_qty": 0,
        "shipping_reserved_qty": 0,
        "shipping_unit_qty": 0,
        "available_to_promise_qty": 0,
        "box_quant_text": "",
        "detail_items": [],
        "detail_json": "[]",
        "main_detail_json": "[]",
        "processing_zone_detail_json": "[]",
        "otg_zone_detail_json": "[]",
        "shipping_reserve_json": "[]",
        "processing_reserve_json": "[]",
        "mixed_composition_label": "",
        "_box_quant_counts": {},
        "_box_quant_seen": set(),
        "_mixed_box_seen": set(),
        "_detail_item_map": {},
        "_main_detail_item_map": {},
        "_processing_zone_detail_item_map": {},
        "_otg_zone_detail_item_map": {},
        "_shipping_reserve_items": [],
        "_shipping_reserve_seen": set(),
        "_processing_reserve_items": [],
        "_processing_reserve_seen": set(),
        "_loose_qty": 0,
        "shipping_quant_labels": [],
        "shipping_quant_text": "",
        "latest_at": None,
    }


def _sku_stock_open_reserve_qty(reserve: WarehouseReserve) -> int:
    return max(int(reserve.qty_reserved or 0) - int(reserve.qty_satisfied or 0), 0)


def _sku_stock_comment_int(pattern: re.Pattern, comment: str | None) -> int:
    match = pattern.search(str(comment or ""))
    if not match:
        return 0
    try:
        return max(int(match.group(1)), 0)
    except (TypeError, ValueError):
        return 0


def _sku_stock_quant_label(order_number: str, item) -> str:
    display_order = format_order_number("shipping", order_number)
    boxes = _sku_stock_comment_int(_SKU_STOCK_BOX_COUNT_RE, getattr(item, "comment", ""))
    box_qty = _sku_stock_comment_int(_SKU_STOCK_BOX_QTY_RE, getattr(item, "comment", ""))
    if boxes > 0 and box_qty > 0:
        return f"{display_order}: {boxes} кор. x {box_qty} шт"
    qty = int(getattr(item, "qty_reserved", 0) or getattr(item, "qty_requested", 0) or 0)
    return f"{display_order}: {qty} шт"


def _sku_stock_box_quant_text(row: dict) -> str:
    if row.get("is_mixed_box"):
        qty = int(row.get("physical_qty") or row.get("box_quant_qty") or 0)
        if qty > 0:
            return f"микс: {qty} шт"
        return "микс"

    counts = row.get("_box_quant_counts") or {}
    parts = []
    for box_qty, box_count in sorted(counts.items(), key=lambda item: (-int(item[1] or 0), int(item[0] or 0))):
        qty_value = int(box_qty or 0)
        count_value = int(box_count or 0)
        if qty_value > 0 and count_value > 0:
            parts.append(f"{count_value} кор. x {qty_value} шт")
    loose_qty = int(row.get("_loose_qty") or 0)
    if loose_qty > 0:
        parts.append(f"без коробов: {loose_qty} шт")
    if not parts:
        return ""
    if len(parts) > 4:
        return "; ".join(parts[:4]) + f"; еще {len(parts) - 4}"
    return "; ".join(parts)


def _sku_stock_detail_place(item: dict) -> str:
    for field in (
        "location_label",
        "location_display",
        "location_name",
        "place_label",
        "place_code",
        "location_code",
        "cell_code",
        "cell",
        "place",
        "address",
        "location",
    ):
        value = str(item.get(field) or "").strip()
        if value and value != "-":
            return value

    zone = str(item.get("zone") or item.get("zone_code") or "").strip()
    row = str(item.get("row") or item.get("row_code") or item.get("aisle") or "").strip()
    section = str(item.get("section") or item.get("section_code") or "").strip()
    tier = str(item.get("tier") or item.get("level") or item.get("level_code") or "").strip()
    cell = str(item.get("cell_number") or item.get("slot") or "").strip()
    parts = []
    if zone:
        parts.append(zone)
    if row:
        parts.append(f"Ряд {row}")
    if section:
        parts.append(f"Секция {section}")
    if tier:
        parts.append(f"Ярус {tier}")
    if cell:
        parts.append(cell)
    return " · ".join(parts) or "-"


def _sku_stock_add_detail_item(
    row: dict,
    *,
    box_code: str,
    pallet_code: str,
    place: str,
    qty: int,
    map_name: str = "_detail_item_map",
) -> None:
    if not box_code or box_code == "-":
        return
    detail_map = row.setdefault(map_name, {})
    key = (box_code, pallet_code or "-", place or "-")
    detail = detail_map.get(key)
    if detail is None:
        detail = {
            "box_code": box_code,
            "pallet_code": pallet_code or "-",
            "place": place or "-",
            "qty": 0,
        }
        detail_map[key] = detail
    detail["qty"] = int(detail.get("qty") or 0) + int(qty or 0)


def _sku_stock_detail_items_from_map(detail_map: dict | None) -> list[dict]:
    return sorted(
        (detail_map or {}).values(),
        key=lambda item: (
            _sku_stock_normalized_text(item.get("place")),
            _sku_stock_normalized_text(item.get("pallet_code")),
            _sku_stock_normalized_text(item.get("box_code")),
        ),
    )


def _sku_stock_finalize_detail_items(row: dict) -> None:
    items = _sku_stock_detail_items_from_map(row.get("_detail_item_map"))
    row["detail_items"] = items
    row["detail_json"] = json.dumps(items, ensure_ascii=False)
    row["main_detail_json"] = json.dumps(
        _sku_stock_detail_items_from_map(row.get("_main_detail_item_map")),
        ensure_ascii=False,
    )
    row["processing_zone_detail_json"] = json.dumps(
        _sku_stock_detail_items_from_map(row.get("_processing_zone_detail_item_map")),
        ensure_ascii=False,
    )
    row["otg_zone_detail_json"] = json.dumps(
        _sku_stock_detail_items_from_map(row.get("_otg_zone_detail_item_map")),
        ensure_ascii=False,
    )
    row["shipping_reserve_json"] = json.dumps(row.get("_shipping_reserve_items") or [], ensure_ascii=False)
    row["processing_reserve_json"] = json.dumps(row.get("_processing_reserve_items") or [], ensure_ascii=False)


def _sku_stock_add_reserve_item(
    row: dict,
    *,
    list_name: str,
    seen_name: str,
    label: str,
    qty: int,
    description: str = "",
    url: str = "",
) -> None:
    label = str(label or "").strip() or "Резерв"
    description = str(description or "").strip()
    url = str(url or "").strip()
    key = (label, description, url, int(qty or 0))
    seen = row.setdefault(seen_name, set())
    if key in seen:
        return
    seen.add(key)
    row.setdefault(list_name, []).append(
        {
            "label": label,
            "qty": int(qty or 0),
            "description": description,
            "url": url,
        }
    )


def _sku_stock_reserve_label(reserve: WarehouseReserve) -> str:
    source_id = str(reserve.source_document_id or reserve.context_id or "").strip()
    source_type = str(reserve.source_document_type or reserve.context_type or "").strip()
    if source_id:
        return source_id
    return source_type or "Резерв"


def _sku_stock_expand_mixed_blocks(rows: list[dict], matched_rows: list[dict]) -> list[dict]:
    mixed_blocks = {
        str(row.get("mixed_block_code") or "").strip()
        for row in matched_rows
        if row.get("is_mixed_box") and str(row.get("mixed_block_code") or "").strip()
    }
    if not mixed_blocks:
        return matched_rows

    matched_ids = {id(row) for row in matched_rows}
    expanded = []
    for row in rows:
        block_code = str(row.get("mixed_block_code") or "").strip()
        if id(row) in matched_ids or block_code in mixed_blocks:
            expanded.append(row)
    return expanded


def _sku_stock_filter_rows(rows: list[dict], *, q: str) -> list[dict]:
    query = str(q or "").strip().lower()
    if not query:
        return rows
    result = []
    for row in rows:
        blob = " ".join(
            str(row.get(field) or "")
            for field in (
                "client_label",
                "client_full_label",
                "sku",
                "name",
                "size",
                "barcode",
                "goods_type",
                "physical_qty",
                "box_quant_text",
                "shipping_reserved_qty",
                "processing_reserved_qty",
                "shipping_quant_text",
            )
        ).lower()
        if query in blob:
            result.append(row)
    return _sku_stock_expand_mixed_blocks(rows, result)


def _sku_stock_normalized_text(value: object | None) -> str:
    return str(value or "").strip().lower()


def _sku_stock_apply_column_filters(rows: list[dict], column_filters: dict[str, str]) -> list[dict]:
    active_filters = {
        field_name: _sku_stock_normalized_text(filter_value)
        for field_name, filter_value in column_filters.items()
        if _sku_stock_normalized_text(filter_value)
    }
    if not active_filters:
        return rows
    result = []
    for row in rows:
        matched = True
        for filter_name, filter_value in active_filters.items():
            row_field = _SKU_STOCK_COLUMN_FILTERS.get(filter_name)
            row_value = _sku_stock_normalized_text(row.get(row_field))
            if filter_value not in row_value:
                matched = False
                break
        if matched:
            result.append(row)
    return _sku_stock_expand_mixed_blocks(rows, result)


def _sku_stock_filter_options(rows: list[dict]) -> dict[str, list[str]]:
    options: dict[str, list[str]] = {}
    for filter_name, row_field in _SKU_STOCK_COLUMN_FILTERS.items():
        values = {
            str(row.get(row_field) or "").strip()
            for row in rows
            if str(row.get(row_field) or "").strip()
        }
        options[filter_name] = sorted(values, key=lambda value: value.lower())[:500]
    return options


def _sku_stock_sort_value(row: dict, sort_key: str):
    row_field = _SKU_STOCK_SORT_FIELDS.get(sort_key, "sku")
    if row_field in _SKU_STOCK_NUMERIC_SORT_FIELDS:
        return int(row.get(row_field) or 0)
    if row_field == "latest_at":
        latest_at = row.get("latest_at")
        return latest_at.timestamp() if latest_at else 0
    return _sku_stock_normalized_text(row.get(row_field))


def _sku_stock_build_query_url(request, **updates) -> str:
    query_params = request.GET.copy()
    query_params.pop("page", None)
    for key, value in updates.items():
        if value in (None, ""):
            query_params.pop(key, None)
        else:
            query_params[key] = value
    encoded = query_params.urlencode()
    return f"{request.path}?{encoded}" if encoded else request.path


def _sku_stock_sort_urls(request, sort_key: str, sort_dir: str) -> dict[str, str]:
    urls = {}
    for key, default_dir in _SKU_STOCK_SORT_DEFAULT_DIR.items():
        next_dir = default_dir
        if sort_key == key:
            next_dir = "desc" if sort_dir == "asc" else "asc"
        urls[key] = _sku_stock_build_query_url(request, sort=key, dir=next_dir)
    return urls


def _sku_stock_summary(rows: list[dict]) -> dict:
    return {
        "sku_count": len(rows),
        "physical_qty": sum(int(row.get("physical_qty") or 0) for row in rows),
        "main_qty": sum(int(row.get("main_qty") or 0) for row in rows),
        "processing_zone_qty": sum(int(row.get("processing_zone_qty") or 0) for row in rows),
        "otg_zone_qty": sum(int(row.get("otg_zone_qty") or 0) for row in rows),
        "shipping_reserved_qty": sum(int(row.get("shipping_reserved_qty") or 0) for row in rows),
        "processing_reserved_qty": sum(int(row.get("processing_reserved_qty") or 0) for row in rows),
        "available_to_promise_qty": sum(int(row.get("available_to_promise_qty") or 0) for row in rows),
    }


def build_sku_stock_page(*, request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    role = get_request_role(request)
    staff_view = request.user.is_staff or is_staff_role(role)
    if not staff_view:
        return HttpResponseForbidden("Доступ запрещен")

    client_id = str(request.GET.get("client") or request.GET.get("agency") or "").strip()
    selected_agency = Agency.objects.filter(pk=int(client_id)).first() if client_id.isdigit() else None
    agencies_by_id = {
        int(agency.id or 0): agency
        for agency in Agency.objects.filter(archived=False).order_by("agn_name", "id")
    }

    rows_by_key: dict[tuple[int, str, str, str, str, int, bool, str], dict] = {}
    physical_rows = _staff_inventory_snapshot_rows(agency=selected_agency)
    box_identities: dict[str, set[tuple[int, str, str, str, str]]] = {}
    for item in physical_rows:
        box_code = str(item.get("box_code") or "").strip()
        if not box_code or box_code == "-":
            continue
        agency_id = int(item.get("agency_id") or 0)
        box_identities.setdefault(box_code, set()).add(
            _sku_stock_group_key(
                agency_id,
                item.get("sku"),
                item.get("size"),
                item.get("barcode"),
                item.get("goods_type"),
            )
        )
    mixed_box_codes = {
        box_code
        for box_code, identities in box_identities.items()
        if len(identities) > 1
    }
    mixed_box_details: dict[str, dict] = {}
    for item in physical_rows:
        box_code = str(item.get("box_code") or "").strip()
        if box_code not in mixed_box_codes:
            continue
        detail = mixed_box_details.get(box_code)
        if detail is None:
            detail = {
                "box_code": box_code,
                "color_index": len(mixed_box_details) % 4,
                "total_qty": 0,
                "parts": {},
            }
            mixed_box_details[box_code] = detail
        qty = int(item.get("qty") or 0)
        detail["total_qty"] += qty
        part_key = (
            str(item.get("sku") or "").strip(),
            str(item.get("name") or "").strip(),
            str(item.get("size") or "").strip(),
            str(item.get("barcode") or "").strip(),
            StockAvailabilityService.normalize_goods_type(item.get("goods_type") or ""),
        )
        detail["parts"][part_key] = int(detail["parts"].get(part_key) or 0) + qty
    for detail in mixed_box_details.values():
        parts = sorted(detail["parts"].items(), key=lambda part: (part[0][0], part[0][1], part[0][3]))
        sku_values = [part[0][0] for part in parts if part[0][0]]
        barcode_values = [part[0][3] for part in parts if part[0][3]]
        goods_type_values = [part[0][4] for part in parts if part[0][4]]
        labels = []
        for (sku, name, size, barcode, goods_type), qty in parts:
            label_bits = []
            if sku:
                label_bits.append(sku)
            if name:
                label_bits.append(name)
            if size and size != "-":
                label_bits.append(size)
            if goods_type:
                label_bits.append(goods_type)
            label = " · ".join(label_bits) or barcode or "товар"
            labels.append(f"{label}: {qty} шт")
        detail["sku_label"] = "; ".join(dict.fromkeys(sku_values)) or "микс"
        detail["name_label"] = "; ".join(labels)
        detail["size_label"] = "микс"
        detail["barcode_label"] = "; ".join(dict.fromkeys(barcode_values)) or "-"
        detail["goods_type_label"] = "; ".join(dict.fromkeys(goods_type_values)) or "микс"
        detail["composition_label"] = "; ".join(labels)

    for item in physical_rows:
        agency = item.get("agency")
        agency_id = int(item.get("agency_id") or 0)
        if agency_id and agency_id not in agencies_by_id and agency is not None:
            agencies_by_id[agency_id] = agency
        qty = int(item.get("qty") or 0)
        box_code = str(item.get("box_code") or "").strip()
        has_box = bool(box_code and box_code != "-")
        is_mixed_box = has_box and box_code in mixed_box_codes
        mixed_detail = mixed_box_details.get(box_code) if is_mixed_box else None
        base_key = _sku_stock_group_key(
            agency_id,
            item.get("sku"),
            item.get("size"),
            item.get("barcode"),
            item.get("goods_type"),
        )
        if mixed_detail:
            part_key = (
                str(item.get("sku") or "").strip(),
                str(item.get("name") or "").strip(),
                str(item.get("size") or "").strip(),
                str(item.get("barcode") or "").strip(),
                StockAvailabilityService.normalize_goods_type(item.get("goods_type") or ""),
            )
            quant_qty = int(mixed_detail["parts"].get(part_key) or qty)
            quant_kind = f"mixed:{box_code}"
        else:
            quant_qty = qty if has_box else 0
            quant_kind = "box" if has_box else "loose"
        key = _sku_stock_row_key(
            base_key,
            box_quant_qty=quant_qty,
            is_mixed_box=is_mixed_box,
            quant_kind=quant_kind,
        )
        row = rows_by_key.get(key)
        if row is None:
            row = _sku_stock_blank_row(
                agency=agency,
                agency_id=agency_id,
                sku=item.get("sku") or "",
                name=item.get("name") or "",
                size=item.get("size") or "",
                barcode=item.get("barcode") or "",
                goods_type=item.get("goods_type") or "",
                box_quant_qty=quant_qty,
                is_mixed_box=is_mixed_box,
                quant_kind=quant_kind,
            )
            if mixed_detail:
                row["mixed_composition_label"] = mixed_detail["composition_label"]
                row["mixed_block_code"] = box_code
                row["mixed_block_label"] = mixed_detail["composition_label"]
                row["mixed_block_color_index"] = int(mixed_detail["color_index"] or 0)
                row["row_css_class"] = f"mixed-quant mixed-quant-{mixed_detail['color_index']}"
            rows_by_key[key] = row
        row["physical_qty"] += qty
        if has_box:
            _sku_stock_add_detail_item(
                row,
                box_code=box_code,
                pallet_code=str(item.get("pallet_code") or item.get("pallet") or "").strip(),
                place=_sku_stock_detail_place(item),
                qty=qty,
            )
        detail_box_code = box_code if has_box else "без короба"
        detail_pallet_code = str(item.get("pallet_code") or item.get("pallet") or "").strip()
        detail_place = _sku_stock_detail_place(item)
        main_qty = int(item.get("stock_main_qty") or 0)
        processing_zone_qty = int(item.get("stock_processing_qty") or 0)
        otg_zone_qty = int(item.get("stock_otg_qty") or 0)
        if main_qty > 0:
            _sku_stock_add_detail_item(
                row,
                box_code=detail_box_code,
                pallet_code=detail_pallet_code,
                place=detail_place,
                qty=main_qty,
                map_name="_main_detail_item_map",
            )
        if processing_zone_qty > 0:
            _sku_stock_add_detail_item(
                row,
                box_code=detail_box_code,
                pallet_code=detail_pallet_code,
                place=detail_place,
                qty=processing_zone_qty,
                map_name="_processing_zone_detail_item_map",
            )
        if otg_zone_qty > 0:
            _sku_stock_add_detail_item(
                row,
                box_code=detail_box_code,
                pallet_code=detail_pallet_code,
                place=detail_place,
                qty=otg_zone_qty,
                map_name="_otg_zone_detail_item_map",
            )
        if has_box:
            seen_boxes = row.setdefault("_box_quant_seen", set())
            if box_code not in seen_boxes:
                seen_boxes.add(box_code)
                quant_counts = row.setdefault("_box_quant_counts", {})
                quant_counts[quant_qty] = int(quant_counts.get(quant_qty) or 0) + 1
                if mixed_detail:
                    row.setdefault("_mixed_box_seen", set()).add(box_code)
        elif qty > 0:
            row["_loose_qty"] = int(row.get("_loose_qty") or 0) + qty
        row["main_qty"] += main_qty
        row["processing_zone_qty"] += processing_zone_qty
        row["otg_zone_qty"] += otg_zone_qty
        row["physical_available_qty"] += int(item.get("available_qty") or 0)
        row["processing_snapshot_reserved_qty"] += int(item.get("processing_reserved_qty") or 0)
        stock_known_qty = (
            int(item.get("stock_main_qty") or 0)
            + int(item.get("stock_processing_qty") or 0)
            + int(item.get("stock_otg_qty") or 0)
        )
        row["other_zone_qty"] += max(qty - stock_known_qty, 0)
        if item.get("updated_at") and (
            not row.get("latest_at") or item["updated_at"] > row["latest_at"]
        ):
            row["latest_at"] = item["updated_at"]

    reserve_qs = (
        WarehouseReserve.objects.select_related("agency", "sku_ref")
        .filter(status__in=_SKU_STOCK_ACTIVE_RESERVE_STATUSES)
        .order_by("id")
    )
    if selected_agency is not None:
        reserve_qs = reserve_qs.filter(agency=selected_agency)

    active_shipping_order_numbers: set[str] = set()
    for reserve in reserve_qs:
        open_qty = _sku_stock_open_reserve_qty(reserve)
        if open_qty <= 0:
            continue
        agency = reserve.agency
        agency_id = int(reserve.agency_id or 0)
        if agency_id and agency_id not in agencies_by_id and agency is not None:
            agencies_by_id[agency_id] = agency
        base_key = _sku_stock_group_key(
            agency_id,
            reserve.sku_code,
            reserve.size,
            reserve.barcode,
            reserve.goods_type,
        )
        reserve_box_qty = _sku_stock_comment_int(
            _SKU_STOCK_BOX_QTY_RE,
            getattr(reserve, "comment", ""),
        )
        key = _sku_stock_row_key(
            base_key,
            box_quant_qty=reserve_box_qty,
            quant_kind="box" if reserve_box_qty > 0 else "reserve",
        )
        row = rows_by_key.get(key)
        if row is None:
            sku_ref = reserve.sku_ref
            row = _sku_stock_blank_row(
                agency=agency,
                agency_id=agency_id,
                sku=reserve.sku_code,
                name=getattr(sku_ref, "name", "") or "",
                size=reserve.size,
                barcode=reserve.barcode,
                goods_type=reserve.goods_type,
                box_quant_qty=reserve_box_qty,
                quant_kind="box" if reserve_box_qty > 0 else "reserve",
            )
            rows_by_key[key] = row
        if reserve.reserve_type == WarehouseReserve.TYPE_SHIPPING:
            row["shipping_reserved_qty"] += open_qty
            if str(reserve.context_id or "").strip():
                active_shipping_order_numbers.update(_shipping_context_lookup_values(reserve.context_id))
        elif reserve.reserve_type == WarehouseReserve.TYPE_PROCESSING:
            row["processing_business_reserved_qty"] += open_qty
            _sku_stock_add_reserve_item(
                row,
                list_name="_processing_reserve_items",
                seen_name="_processing_reserve_seen",
                label=_sku_stock_reserve_label(reserve),
                qty=open_qty,
                description=str(
                    getattr(reserve, "comment", "")
                    or reserve.context_type
                    or reserve.source_document_type
                    or ""
                ).strip(),
            )

    if active_shipping_order_numbers:
        orders = (
            ShippingOrder.objects.filter(number__in=active_shipping_order_numbers)
            .select_related("agency")
            .prefetch_related("items")
        )
        for order in orders:
            agency_id = int(order.agency_id or 0)
            for item in order.items.all():
                box_qty = _sku_stock_comment_int(
                    _SKU_STOCK_BOX_QTY_RE,
                    getattr(item, "comment", ""),
                )
                base_key = _sku_stock_group_key(
                    agency_id,
                    getattr(item, "sku_code", ""),
                    getattr(item, "size", ""),
                    getattr(item, "barcode", ""),
                    getattr(item, "goods_type", ""),
                )
                key = _sku_stock_row_key(
                    base_key,
                    box_quant_qty=box_qty,
                    quant_kind="box" if box_qty > 0 else "reserve",
                )
                row = rows_by_key.get(key)
                if row is None:
                    row = _sku_stock_blank_row(
                        agency=order.agency,
                        agency_id=agency_id,
                        sku=getattr(item, "sku_code", ""),
                        name=getattr(item, "name", ""),
                        size=getattr(item, "size", ""),
                        barcode=getattr(item, "barcode", ""),
                        goods_type=getattr(item, "goods_type", ""),
                        box_quant_qty=box_qty,
                        quant_kind="box" if box_qty > 0 else "reserve",
                    )
                    rows_by_key[key] = row
                label = _sku_stock_quant_label(order.number, item)
                if label not in row["shipping_quant_labels"]:
                    row["shipping_quant_labels"].append(label)
                _sku_stock_add_reserve_item(
                    row,
                    list_name="_shipping_reserve_items",
                    seen_name="_shipping_reserve_seen",
                    label=format_order_number("shipping", order.number),
                    qty=int(getattr(item, "qty_reserved", 0) or getattr(item, "qty_requested", 0) or 0),
                    description=label,
                    url=f"/shipping/{order.pk}/",
                )

    for row in rows_by_key.values():
        row["box_quant_text"] = _sku_stock_box_quant_text(row)
        _sku_stock_finalize_detail_items(row)
        row["processing_reserved_qty"] = max(
            int(row.get("processing_snapshot_reserved_qty") or 0),
            int(row.get("processing_business_reserved_qty") or 0),
        )
        row["shipping_quant_text"] = "; ".join(row["shipping_quant_labels"][:4])
        if len(row["shipping_quant_labels"]) > 4:
            row["shipping_quant_text"] += f"; еще {len(row['shipping_quant_labels']) - 4}"
        row["available_to_promise_qty"] = max(
            int(row.get("physical_qty") or 0)
            - int(row.get("processing_reserved_qty") or 0)
            - int(row.get("shipping_reserved_qty") or 0),
            0,
        )

    rows = [
        row
        for row in rows_by_key.values()
        if int(row.get("physical_qty") or 0) > 0
        or int(row.get("shipping_reserved_qty") or 0) > 0
        or int(row.get("processing_reserved_qty") or 0) > 0
    ]
    q = str(request.GET.get("q") or "").strip()
    rows = _sku_stock_filter_rows(rows, q=q)
    column_filters = {
        key: str(request.GET.get(key) or "").strip()
        for key in _SKU_STOCK_COLUMN_FILTERS
    }
    column_filter_options = _sku_stock_filter_options(rows)
    rows = _sku_stock_apply_column_filters(rows, column_filters)
    sort_key = str(request.GET.get("sort") or "sku").strip()
    if sort_key not in _SKU_STOCK_SORT_FIELDS:
        sort_key = "sku"
    sort_dir = str(
        request.GET.get("dir") or _SKU_STOCK_SORT_DEFAULT_DIR.get(sort_key, "asc")
    ).strip().lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = _SKU_STOCK_SORT_DEFAULT_DIR.get(sort_key, "asc")
    mixed_block_sort_values = {}
    for row in rows:
        block_code = str(row.get("mixed_block_code") or "").strip()
        if block_code:
            value = _sku_stock_sort_value(row, sort_key)
            current = mixed_block_sort_values.get(block_code)
            if current is None or value < current:
                mixed_block_sort_values[block_code] = value
    rows.sort(
        key=lambda row: (
            mixed_block_sort_values.get(str(row.get("mixed_block_code") or "").strip(), _sku_stock_sort_value(row, sort_key)),
            _sku_stock_normalized_text(row.get("client_label")),
            _sku_stock_normalized_text(row.get("mixed_block_code")),
            _sku_stock_normalized_text(row.get("sku")),
            _sku_stock_normalized_text(row.get("name")),
            _sku_stock_normalized_text(row.get("size")),
            _sku_stock_normalized_text(row.get("barcode")),
            _sku_stock_normalized_text(row.get("goods_type")),
            0 if row.get("quant_kind") == "box" else 1,
            0 if not row.get("is_mixed_box") else 1,
            -int(row.get("box_quant_qty") or 0),
        ),
        reverse=(sort_dir == "desc"),
    )

    query_params = request.GET.copy()
    query_params.pop("page", None)
    reset_params = {}
    if selected_agency is not None:
        reset_params["client"] = str(selected_agency.id)

    return {
        "template_name": "sklad/sku_stock.html",
        "context": {
            "rows": rows,
            "summary": _sku_stock_summary(rows),
            "q": q,
            "selected_agency": selected_agency,
            "client_options": list(agencies_by_id.values()),
            "column_filters": column_filters,
            "column_filter_options": column_filter_options,
            "sku_stock_sort_key": sort_key,
            "sku_stock_sort_dir": sort_dir,
            "sku_stock_sort_urls": _sku_stock_sort_urls(request, sort_key, sort_dir),
            "sku_stock_has_filters": bool(q or any(column_filters.values())),
            "page_query": query_params.urlencode(),
            "reset_url": f"{request.path}?{urlencode(reset_params)}" if reset_params else request.path,
        },
    }


def _aggregate_inventory_journal_snapshot_rows(rows: list[dict]) -> list[dict]:
    identity_fields = (
        "source_order_id",
        "source_order_type",
        "order_id",
        "order_type",
        "container_id",
        "parent_container_id",
        "location_id",
        "active_context_type",
        "active_context_id",
        "active_context_status",
        "last_event_type",
        "last_event_context_type",
        "last_event_context_id",
        "warehouse_state_code",
        "is_processing_source_box",
        "order_type_code",
        "order_type_label",
        "client_label",
        "agency_id",
        "sku",
        "barcode",
        "name",
        "size",
        "box_size",
        "box_weight",
        "goods_type",
        "is_fbs_stock",
        "box_code",
        "pallet_code",
        "row_no",
        "section_no",
        "tier_no",
        "cell_no",
        "zone",
        "location",
    )
    grouped: dict[tuple, dict] = {}
    for row in rows:
        source_row_count = max(int(row.get("_journal_source_row_count") or 1), 1)
        key = tuple(row.get(field) for field in identity_fields)
        existing = grouped.get(key)
        if existing is None:
            item = dict(row)
            item.pop("_journal_source_row_count", None)
            item.pop("_journal_first_qty", None)
            item["snapshot_row_count"] = source_row_count
            item["is_aggregated_snapshot_row"] = source_row_count > 1
            grouped[key] = item
            continue
        existing["snapshot_row_count"] = (
            int(existing.get("snapshot_row_count") or 1) + source_row_count
        )
        existing["is_aggregated_snapshot_row"] = True
        for field_name in _JOURNAL_STOCK_METRIC_FIELDS:
            existing[field_name] = int(existing.get(field_name) or 0) + int(row.get(field_name) or 0)
        if row.get("created_at") and (
            not existing.get("created_at") or row["created_at"] > existing["created_at"]
        ):
            existing["created_at"] = row["created_at"]
        if not int(existing.get("snapshot_id") or 0) and int(row.get("snapshot_id") or 0):
            existing["snapshot_id"] = int(row.get("snapshot_id") or 0)
    return list(grouped.values())


def _journal_row_is_processing_consumed(row: dict) -> bool:
    return str(row.get("warehouse_state_code") or "").strip().lower() == _JOURNAL_PROCESSING_CONSUMED_STATE


def _journal_row_identity(row: dict, fallback_index: int) -> tuple:
    snapshot_id = int(row.get("snapshot_id") or 0)
    if snapshot_id:
        return ("snapshot", snapshot_id)
    return (
        "row",
        fallback_index,
        row.get("agency_id"),
        row.get("sku"),
        row.get("size"),
        row.get("goods_type"),
        row.get("box_code"),
        row.get("pallet_code"),
        row.get("order_type"),
        row.get("order_id"),
    )


def _journal_status_parts(row: dict) -> list[tuple[str, str, int]]:
    if row.get("is_processing_consumed"):
        return [("processing_consumed", "Списали", int(row.get("qty") or 0))]
    if row.get("is_fbs_stock"):
        parts = []
        for field_name, label in (
            ("fbs_available_qty", "Доступно FBS"),
            ("fbs_reserved_qty", "Резерв FBS"),
        ):
            qty_value = int(row.get(field_name) or 0)
            if qty_value > 0:
                parts.append((field_name, label, qty_value))
        return parts or [("fbs_qty", "Остаток FBS", int(row.get("fbs_qty") or 0))]
    parts = []
    for field_name, label in (
        ("available_qty", "Доступно"),
        ("shipping_reserved_qty", "Резерв OTG"),
        ("processing_reserved_qty", "Резерв OBR"),
        ("processing_in_progress_qty", "В обработке"),
    ):
        qty_value = int(row.get(field_name) or 0)
        if qty_value > 0:
            parts.append((field_name, label, qty_value))
    if parts:
        return parts
    for field_name, label in (
        ("stock_main_qty", "На складе"),
        ("stock_processing_qty", "Склад OBR"),
        ("stock_otg_qty", "Склад OTG"),
    ):
        qty_value = int(row.get(field_name) or 0)
        if qty_value > 0:
            return [(field_name, label, qty_value)]
    return [("empty", "Нет доступного остатка", int(row.get("qty") or 0))]


def _journal_box_size_hint(value: object | None) -> str:
    text = str(value or "").strip()
    if not text or text == "-":
        return "-"
    parts = [
        part.strip()
        for part in re.split(r"[\\/xхXХ×*]+", text)
        if part.strip()
    ]
    if len(parts) >= 3:
        return f"Ширина: {parts[0]} мм; Высота: {parts[1]} мм; Глубина: {parts[2]} мм"
    if len(parts) == 2:
        return f"Размер: {parts[0]} мм × {parts[1]} мм"
    return f"Размер: {text}"


def _journal_box_weight_hint(value: object | None) -> str:
    text = str(value or "").strip()
    if not text or text == "-":
        return "-"
    normalized = text.replace(",", ".")
    try:
        kg_value = float(normalized) / 1000
    except ValueError:
        return f"Вес: {text}"
    kg_text = f"{kg_value:.3f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"Вес: {kg_text or '0'} кг"


_JOURNAL_GOODS_TYPE_LABELS = {
    "gv": "Готовый",
    "br": "Брак",
    "op": "Оптовый",
    "no": "Не обработанный",
    "vz": "Возврат",
    "votg": "Возврат с отгрузки",
    "готовый": "Готовый",
    "брак": "Брак",
    "оптовый": "Оптовый",
    "не обработанный": "Не обработанный",
    "возврат": "Возврат",
    "возврат с отгрузки": "Возврат с отгрузки",
}


def _journal_goods_type_hint(value: object | None) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "-":
        return "-"
    label = _JOURNAL_GOODS_TYPE_LABELS.get(raw.lower(), raw)
    if label.lower() == raw.lower():
        return f"Тип товара: {label}"
    return f"Тип товара: {label} ({raw})"


def _journal_status_hint(row: dict) -> str:
    label = str(row.get("stock_status_label") or "").strip()
    if row.get("stock_status_key") == "processing_consumed":
        order_display = str(row.get("order_display") or "-").strip() or "-"
        return f"Списано в: {order_display}"
    return f"Статус: {label or '-'}"


def _expand_inventory_journal_status_rows(rows: list[dict]) -> list[dict]:
    expanded_rows = []
    for index, row in enumerate(rows):
        original_key = _journal_row_identity(row, index)
        original_metrics = {
            field_name: int(row.get(field_name) or 0)
            for field_name in _JOURNAL_STOCK_METRIC_FIELDS
        }
        for status_key, status_label, status_qty in _journal_status_parts(row):
            status_row = dict(row)
            status_row["_journal_original_key"] = original_key
            for field_name, value in original_metrics.items():
                status_row[f"_journal_original_{field_name}"] = value
            for field_name in _JOURNAL_STATUS_FIELDS:
                status_row[field_name] = 0
            if status_key in _JOURNAL_STATUS_FIELDS:
                status_row[status_key] = status_qty
            if row.get("is_fbs_stock"):
                status_row["fbs_qty"] = status_qty
            status_row["qty"] = status_qty
            status_row["stock_status_key"] = status_key
            status_row["stock_status_label"] = status_label
            status_row["stock_status_qty"] = status_qty
            status_row["stock_status_hint"] = _journal_status_hint(status_row)
            status_row["box_size_hint"] = _journal_box_size_hint(status_row.get("box_size"))
            status_row["box_weight_hint"] = _journal_box_weight_hint(status_row.get("box_weight"))
            status_row["goods_type_hint"] = _journal_goods_type_hint(status_row.get("goods_type"))
            status_row["processing_work_qty"] = int(status_row.get("processing_reserved_qty") or 0) + int(
                status_row.get("processing_in_progress_qty") or 0
            )
            expanded_rows.append(status_row)
    return expanded_rows


def _shorten_ip_name(name: str) -> str:
    if not name:
        return "-"
    normalized = _IP_PREFIX_RE.sub("ИП", name)
    return " ".join(normalized.split())


def _agency_journal_label(agency: Agency | None) -> str:
    if not agency:
        return "-"
    for value in (
        getattr(agency, "short_name", None),
        getattr(agency, "agn_name", None),
        getattr(agency, "fio_agn", None),
        str(agency or ""),
    ):
        text = str(value or "").strip()
        if text:
            return _shorten_ip_name(text)
    return "-"


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
    if zone_code == "FBS":
        location_parts = [part.strip() for part in location_text.split("·") if part.strip()]
        for value in location_parts:
            if value.upper().startswith("FBS@"):
                return value
        for value in location_parts:
            if value.upper() != "FBS":
                return value
        return "FBS"
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


def _processing_application_payload(entries: list[OrderAuditEntry]) -> dict:
    latest_draft = None
    current_payload = None
    for entry in reversed(entries):
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        if entry.action != "status" and not any(
            payload.get(key) for key in ("status", "status_label", "submit_action", "processing_stage")
        ):
            continue
        status_value = str(payload.get("status") or payload.get("submit_action") or "").strip().lower()
        status_label = str(payload.get("status_label") or "").strip().lower()
        is_draft = processing_stage_from_payload(payload) == PROCESSING_STAGE_DRAFT or (
            status_value == "draft" or "черновик" in status_label
        )
        if is_draft:
            latest_draft = latest_draft or payload
            continue
        current_payload = payload
        break
    current_payload = dict(current_payload or latest_draft or {})
    for key in ("stock_rows", "cards", "size_rows"):
        for entry in reversed(entries):
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if key in payload:
                current_payload[key] = payload.get(key)
                break
    return current_payload


def _processing_application_items(payload: dict) -> list[dict]:
    raw_rows = payload.get("stock_rows") or []
    if not isinstance(raw_rows, list) or not raw_rows:
        raw_rows = []
        cards = payload.get("cards") or []
        if isinstance(cards, list):
            for card in cards:
                if not isinstance(card, dict):
                    continue
                card_article = str(card.get("article") or "").strip()
                card_name = str(card.get("name") or card.get("product_name") or "").strip()
                for row in card.get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    item = dict(row)
                    item.setdefault("article", card_article)
                    item.setdefault("name", card_name)
                    raw_rows.append(item)
    if not raw_rows:
        size_rows = payload.get("size_rows") or []
        raw_rows = size_rows if isinstance(size_rows, list) else []

    grouped: dict[tuple[str, str, str], dict] = {}
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        sku_code = str(
            row.get("article")
            or row.get("sku_code")
            or row.get("sku")
            or payload.get("article")
            or ""
        ).strip()
        if not sku_code:
            continue
        size = str(row.get("size") or row.get("size_value") or "").strip()
        goods_type = StockAvailabilityService.normalize_goods_type(
            row.get("goods_type") or payload.get("goods_type") or ""
        )
        qty = _parse_qty_value(row.get("qty"))
        if qty is None:
            qty = _parse_qty_value(row.get("recount_qty")) or 0
        if qty <= 0:
            continue
        key = (sku_code.lower(), size.lower(), goods_type)
        item = grouped.setdefault(
            key,
            {
                "sku_code": sku_code,
                "name": str(row.get("name") or row.get("product_name") or payload.get("product_name") or "").strip(),
                "size": size,
                "goods_type": goods_type,
                "request_qty": 0,
            },
        )
        item["request_qty"] += qty
    return list(grouped.values())


def _processing_active_request_groups(agency: Agency) -> list[dict]:
    order_ids = list(
        OrderAuditEntry.objects.filter(order_type="processing", agency=agency)
        .values_list("order_id", flat=True)
        .distinct()
    )
    if not order_ids:
        return []
    entries = list(
        OrderAuditEntry.objects.filter(order_type="processing", order_id__in=order_ids)
        .only("id", "order_id", "action", "agency_id", "payload", "created_at")
        .order_by("order_id", "created_at", "id")
    )
    grouped_entries: dict[str, list[OrderAuditEntry]] = {}
    for entry in entries:
        grouped_entries.setdefault(str(entry.order_id or "").strip(), []).append(entry)

    groups: list[dict] = []
    for order_entries in grouped_entries.values():
        payload = _processing_application_payload(order_entries)
        if not payload or processing_is_cancelled(payload) or processing_is_done(payload):
            continue
        stage = processing_stage_from_payload(payload)
        status_value = str(payload.get("status") or payload.get("submit_action") or "").strip().lower()
        if stage == PROCESSING_STAGE_DRAFT or (not stage and status_value not in {"send", "submitted"}):
            continue
        for item in _processing_application_items(payload):
            groups.append(
                {
                    "agency_id": agency.id,
                    "sku_code": item["sku_code"],
                    "name": item.get("name") or "",
                    "size": item.get("size") or "",
                    "goods_type": item.get("goods_type") or "",
                    "request_qty": int(item.get("request_qty") or 0),
                }
            )
    return groups


def _shipping_active_request_groups(agency: Agency) -> list[dict]:
    groups = list(
        ShippingOrderItem.objects.filter(
            order__agency=agency,
            order__status__in=_CLIENT_ACTIVE_SHIPPING_STATUSES,
            qty_requested__gt=0,
        )
        .values("order__agency_id", "sku_code", "name", "size", "goods_type")
        .annotate(
            requested_qty=Sum("qty_requested"),
            shipped_qty=Sum("qty_shipped"),
            updated_at_max=Max("updated_at"),
        )
    )
    result: list[dict] = []
    for item in groups:
        request_qty = max(int(item.get("requested_qty") or 0) - int(item.get("shipped_qty") or 0), 0)
        if request_qty <= 0:
            continue
        result.append(
            {
                "agency_id": item.get("order__agency_id"),
                "sku_code": item.get("sku_code"),
                "name": item.get("name") or "",
                "size": item.get("size") or "",
                "goods_type": item.get("goods_type") or "",
                "request_qty": request_qty,
                "updated_at_max": item.get("updated_at_max"),
            }
        )
    return result


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


def _request_base_key(agency_id: int | None, sku: str | None, size: str | None) -> tuple[int, str, str]:
    return (
        int(agency_id or 0),
        str(sku or "").strip().lower(),
        str(size or "").strip().lower(),
    )


def _client_row_accounted_qty(row: dict) -> int:
    processing_qty = int(row.get("processing_reserved_qty") or 0) + int(
        row.get("processing_in_progress_qty") or 0
    )
    shipping_qty = max(
        int(row.get("shipping_work_qty") or 0),
        int(row.get("shipping_reserved_qty") or 0),
    )
    return (
        int(row.get("available_qty") or 0)
        + processing_qty
        + shipping_qty
        + int(row.get("problem_qty") or 0)
    )


def _allocate_client_request_groups(rows: list[dict], groups: list[dict], *, field: str) -> None:
    by_base: dict[tuple[int, str, str], list[dict]] = {}
    by_exact: dict[tuple[int, str, str, str], list[dict]] = {}
    for row in rows:
        row[field] = 0
        base_key = _request_base_key(row.get("agency_id"), row.get("sku"), row.get("size"))
        exact_key = _reserve_key(
            row.get("agency_id"),
            row.get("sku"),
            row.get("size"),
            row.get("goods_type"),
        )
        by_base.setdefault(base_key, []).append(row)
        by_exact.setdefault(exact_key, []).append(row)

    for item in groups:
        request_qty = int(item.get("request_qty") or 0)
        if request_qty <= 0:
            continue
        base_key = _request_base_key(item.get("agency_id"), item.get("sku_code"), item.get("size"))
        exact_key = _reserve_key(
            item.get("agency_id"),
            item.get("sku_code"),
            item.get("size"),
            item.get("goods_type"),
        )
        normalized_goods_type = StockAvailabilityService.normalize_goods_type(item.get("goods_type") or "")
        candidates = by_exact.get(exact_key, []) if normalized_goods_type else []
        if not candidates:
            candidates = by_base.get(base_key, [])
        if not candidates:
            continue
        candidates = sorted(candidates, key=_client_row_accounted_qty, reverse=True)
        remaining = request_qty
        for row in candidates:
            capacity = max(_client_row_accounted_qty(row) - int(row.get(field) or 0), 0)
            allocated = min(remaining, capacity)
            row[field] = int(row.get(field) or 0) + allocated
            remaining -= allocated
            if remaining <= 0:
                break
        if remaining > 0:
            candidates[0][field] = int(candidates[0].get(field) or 0) + remaining


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


def _shipping_context_lookup_values(context_id: object | None) -> set[str]:
    raw = str(context_id or "").strip()
    if not raw:
        return set()
    raw_upper = raw.upper()
    values = {raw, raw_upper}
    otg_match = re.match(r"^(\d+)_OTG$", raw_upper)
    if otg_match:
        number_part = otg_match.group(1)
    elif raw_upper.startswith("SO-"):
        number_part = raw_upper[3:]
    else:
        number_part = raw_upper
    if number_part.isdigit():
        normalized_number = int(number_part)
        values.add(str(normalized_number))
        values.add(f"{normalized_number}_OTG")
        values.add(f"SO-{normalized_number:06d}")
    return values


def _is_canceled_shipping_context(
    *,
    context_type: str,
    context_id: str,
    canceled_shipping_context_ids: set[str],
) -> bool:
    if context_type != "shipping" or not context_id or not canceled_shipping_context_ids:
        return False
    return bool(_shipping_context_lookup_values(context_id) & canceled_shipping_context_ids)


def _current_context_for_display(
    row: dict,
    *,
    canceled_shipping_context_ids: set[str],
) -> tuple[str, str]:
    active_type = str(row.get("active_context_type") or "").strip().lower()
    active_id = str(row.get("active_context_id") or "").strip()
    active_status = str(row.get("active_context_status") or "").strip().lower()
    if (
        active_type in _JOURNAL_TARGET_CONTEXT_TYPES
        and active_id
        and active_status not in _JOURNAL_CANCELED_OPERATION_STATUSES
        and not _is_canceled_shipping_context(
            context_type=active_type,
            context_id=active_id,
            canceled_shipping_context_ids=canceled_shipping_context_ids,
        )
    ):
        return active_type, active_id

    last_event_type = str(row.get("last_event_type") or "").strip()
    if last_event_type in _JOURNAL_CONTEXT_CLEARING_EVENT_TYPES:
        return "", ""

    state_code = str(row.get("warehouse_state_code") or "").strip().lower()
    event_type = str(row.get("last_event_context_type") or "").strip().lower()
    event_id = str(row.get("last_event_context_id") or "").strip()
    if (
        event_type == "processing"
        and event_id
        and state_code not in _JOURNAL_EDIT_PROCESSING_STATES
    ):
        return "", ""
    if (
        event_type in _JOURNAL_TARGET_CONTEXT_TYPES
        and event_id
        and not _is_canceled_shipping_context(
            context_type=event_type,
            context_id=event_id,
            canceled_shipping_context_ids=canceled_shipping_context_ids,
        )
    ):
        return event_type, event_id
    return "", ""


def _canceled_shipping_context_ids(rows: list[dict]) -> set[str]:
    lookup_values: set[str] = set()
    for row in rows:
        for type_key, id_key in (
            ("active_context_type", "active_context_id"),
            ("last_event_context_type", "last_event_context_id"),
        ):
            context_type = str(row.get(type_key) or "").strip().lower()
            if context_type != "shipping":
                continue
            lookup_values.update(_shipping_context_lookup_values(row.get(id_key)))
    if not lookup_values:
        return set()
    return set(
        ShippingOrder.objects.filter(
            number__in=lookup_values,
            status=ShippingOrder.STATUS_CANCELED,
        ).values_list("number", flat=True)
    )


def _movement_order_display(row: dict, *, canceled_shipping_context_ids: set[str] | None = None) -> str:
    source_type = str(row.get("source_order_type") or row.get("order_type") or "").strip().lower()
    source_id = str(row.get("source_order_id") or row.get("order_id") or "").strip()
    source_display = format_order_number(source_type, source_id)

    current_type, current_id = _current_context_for_display(
        row,
        canceled_shipping_context_ids=canceled_shipping_context_ids or set(),
    )
    if current_type not in _JOURNAL_TARGET_CONTEXT_TYPES or not current_id:
        return source_display

    current_display = format_order_number(current_type, current_id)
    if current_display == "-" or current_display == source_display:
        return source_display
    if source_display == "-":
        return current_display
    return f"{source_display} -> {current_display}"


def _journal_edit_lock_reason(
    row: dict,
    *,
    canceled_shipping_context_ids: set[str] | None = None,
) -> str:
    canceled_shipping_context_ids = canceled_shipping_context_ids or set()
    current_type, current_id = _current_context_for_display(
        row,
        canceled_shipping_context_ids=canceled_shipping_context_ids,
    )
    order_label = format_order_number(current_type, current_id) if current_type and current_id else "-"
    state_code = str(row.get("warehouse_state_code") or "").strip().lower()
    if state_code in _JOURNAL_EDIT_RESERVED_STATES:
        return f"Нельзя редактировать: товар в резерве, заявка {order_label}"
    if state_code in _JOURNAL_EDIT_SHIPPING_STATES or current_type == "shipping":
        return f"Нельзя редактировать: товар в отгрузке, заявка {order_label}"
    if state_code in _JOURNAL_EDIT_PROCESSING_STATES or current_type == "processing":
        return f"Нельзя редактировать: товар в обработке, заявка {order_label}"
    return ""


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
    if filter_name == "f_location":
        raw_os_location = ""
        if str(row.get("zone") or "").strip().upper() == "OS":
            row_no = int(row.get("row_no") or 0)
            section_no = int(row.get("section_no") or 0)
            tier_no = int(row.get("tier_no") or 0)
            cell_no = int(row.get("cell_no") or 0)
            if row_no and section_no and tier_no and cell_no:
                raw_os_location = f"OS-{row_no}/{section_no}-{tier_no}-{cell_no}"
        return " ".join(
            part
            for part in [
                str(row.get("location_short") or "").strip(),
                str(row.get("location") or "").strip(),
                str(row.get("zone") or "").strip(),
                raw_os_location,
            ]
            if part and part != "-"
        )
    field_name = _JOURNAL_COLUMN_FILTERS.get(filter_name)
    if not field_name:
        return ""
    return str(row.get(field_name) or "").strip()


def _journal_filter_option_text(row: dict, filter_name: str) -> str:
    if filter_name == "f_order":
        return str(row.get("order_display") or "").strip()
    if filter_name == "f_location":
        return str(row.get("location_short") or "").strip()
    return _journal_filter_text(row, filter_name)


def _build_journal_filter_options(
    rows: list[dict],
    *,
    preferred_filters: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    preferred_filters = preferred_filters or {}
    matched_options: dict[str, list[str]] = {
        filter_name: [] for filter_name in _JOURNAL_COLUMN_FILTERS
    }
    fallback_options: dict[str, list[str]] = {
        filter_name: [] for filter_name in _JOURNAL_COLUMN_FILTERS
    }
    seen: dict[str, set[str]] = {filter_name: set() for filter_name in _JOURNAL_COLUMN_FILTERS}
    for row in rows:
        for filter_name in _JOURNAL_COLUMN_FILTERS:
            value = _journal_filter_option_text(row, filter_name).strip()
            if not value or value == "-":
                continue
            normalized = _journal_normalized_text(value)
            if not normalized or normalized in seen[filter_name]:
                continue
            seen[filter_name].add(normalized)
            preferred = _journal_normalized_text(preferred_filters.get(filter_name))
            if preferred and preferred in normalized:
                if len(matched_options[filter_name]) < _JOURNAL_FILTER_OPTION_LIMIT:
                    matched_options[filter_name].append(value)
            elif len(fallback_options[filter_name]) < _JOURNAL_FILTER_OPTION_LIMIT:
                fallback_options[filter_name].append(value)
    options: dict[str, list[str]] = {}
    for filter_name in _JOURNAL_COLUMN_FILTERS:
        matched = sorted(matched_options[filter_name], key=_journal_normalized_text)
        fallback = sorted(fallback_options[filter_name], key=_journal_normalized_text)
        options[filter_name] = [
            *matched,
            *fallback[: max(_JOURNAL_FILTER_OPTION_LIMIT - len(matched), 0)],
        ]
    return options


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
        "stock_main_qty",
        "stock_processing_qty",
        "stock_otg_qty",
        "available_qty",
        "processing_work_qty",
        "processing_reserved_qty",
        "processing_in_progress_qty",
        "shipping_reserved_qty",
        "fbs_qty",
        "fbs_available_qty",
        "fbs_reserved_qty",
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
    total_stock_main_qty = 0
    total_stock_processing_qty = 0
    total_stock_otg_qty = 0
    total_fbs_qty = 0
    total_fbs_available_qty = 0
    total_available_qty = 0
    total_processing_reserved_qty = 0
    total_processing_in_progress_qty = 0
    total_shipping_reserved_qty = 0
    counted_metric_keys = set()
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
        metric_key = row.get("_journal_original_key") or ("visible", id(row))
        if metric_key in counted_metric_keys:
            continue
        counted_metric_keys.add(metric_key)
        total_qty += int(row.get("_journal_original_qty", row.get("qty") or 0) or 0)
        total_stock_main_qty += int(row.get("_journal_original_stock_main_qty", row.get("stock_main_qty") or 0) or 0)
        total_stock_processing_qty += int(
            row.get("_journal_original_stock_processing_qty", row.get("stock_processing_qty") or 0) or 0
        )
        total_stock_otg_qty += int(row.get("_journal_original_stock_otg_qty", row.get("stock_otg_qty") or 0) or 0)
        total_fbs_qty += int(
            row.get("_journal_original_fbs_qty", row.get("fbs_qty") or 0) or 0
        )
        total_fbs_available_qty += int(
            row.get(
                "_journal_original_fbs_available_qty",
                row.get("fbs_available_qty") or 0,
            )
            or 0
        )
        total_available_qty += int(row.get("_journal_original_available_qty", row.get("available_qty") or 0) or 0)
        total_processing_reserved_qty += int(
            row.get("_journal_original_processing_reserved_qty", row.get("processing_reserved_qty") or 0) or 0
        )
        total_processing_in_progress_qty += int(
            row.get("_journal_original_processing_in_progress_qty", row.get("processing_in_progress_qty") or 0) or 0
        )
        total_shipping_reserved_qty += int(
            row.get("_journal_original_shipping_reserved_qty", row.get("shipping_reserved_qty") or 0) or 0
        )
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
        "total_stock_main_qty": total_stock_main_qty,
        "total_stock_processing_qty": total_stock_processing_qty,
        "total_stock_otg_qty": total_stock_otg_qty,
        "total_fbs_qty": total_fbs_qty,
        "total_fbs_available_qty": total_fbs_available_qty,
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
    storage_contours = _journal_unique_values(rows, "storage_contour_label")
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
        "storage_contour_label": (
            storage_contours[0] if len(storage_contours) == 1 else ""
        ),
        "location_label": locations[0] if len(locations) == 1 else "",
        "order_label": distinct_orders[0] if len(distinct_orders) == 1 else "",
        "sku_labels": sku_labels[:6],
        "summary": summary,
    }


def _daily_problem_control_context(
    *,
    boxes: list[dict],
    pallets: list[dict],
    missing_boxes: list[dict],
) -> dict:
    from todo.models import Task

    target_day = timezone.localdate()
    task = (
        Task.objects.filter(route=daily_problem_control_route(target_day))
        .select_related("assigned_to")
        .first()
    )
    issue_count = len(boxes) + len(pallets) + len(missing_boxes)
    return {
        "date_label": target_day.strftime("%d.%m.%Y"),
        "scheduled_time_label": "09:00",
        "box_count": len(boxes),
        "pallet_count": len(pallets),
        "missing_box_count": len(missing_boxes),
        "in_check_count": (
            sum(1 for row in boxes if int(row.get("operation_id") or 0) > 0)
            + sum(1 for row in missing_boxes if int(row.get("assigned_to_id") or 0) > 0)
        ),
        "issue_count": issue_count,
        "status_label": (
            "Требует проверки"
            if issue_count
            else "Проблем не найдено"
            if task is not None
            else "Автопроверка еще не запускалась"
        ),
        "status_tone": "error" if issue_count else "success" if task is not None else "muted",
        "task_id": int(task.id) if task is not None else 0,
        "task_url": f"/todo/{task.id}/" if task is not None else "",
        "task_status_label": task.get_status_display() if task is not None else "Ожидает запуска",
        "task_assigned_to": (
            str(getattr(task.assigned_to, "full_name", "") or "").strip()
            if task is not None
            else ""
        ),
        "last_run_at": getattr(task, "updated_at", None) if task is not None else None,
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
    q = (request.GET.get("q") or "").strip()
    column_filters = {
        key: (request.GET.get(key) or "").strip()
        for key in _JOURNAL_COLUMN_FILTERS
    }
    multi_column_filters: dict[str, list[str]] = {}
    multi_column_filter_keys: dict[str, set[str]] = {}
    for filter_name in _JOURNAL_COLUMN_FILTERS:
        raw_values = list(request.GET.getlist(f"{filter_name}_values"))
        if filter_name == "f_location":
            raw_values = [*request.GET.getlist("f_locations"), *raw_values]
        selected_values: list[str] = []
        selected_keys: set[str] = set()
        for raw_value in raw_values:
            value = " ".join(str(raw_value or "").strip().split())
            normalized = _journal_normalized_text(value)
            if not normalized or normalized in selected_keys:
                continue
            selected_keys.add(normalized)
            selected_values.append(value)
        multi_column_filters[filter_name] = selected_values
        multi_column_filter_keys[filter_name] = selected_keys
    rows = []
    order_type_codes = {
        "receiving": "ПРМ",
        "processing": "ОБР",
        "shipping": "ОТГ",
        "fbs": "FBS",
    }
    order_type_titles = {
        "receiving": "Приемка",
        "processing": "Обработка",
        "shipping": "Отгрузка",
        "fbs": "FBS",
    }
    if staff_view:
        warehouse_rows = _staff_inventory_snapshot_rows(
            agency=client_agency,
            box_filter=column_filters.get("f_box", ""),
            pallet_filter=column_filters.get("f_pallet", ""),
            container_search=_journal_container_search(q),
            aggregate_for_journal=True,
        )
        storage_rows = list(warehouse_rows)
    else:
        warehouse_rows = StockAvailabilityService.stock_rows_with_availability(agency=client_agency)
        storage_rows = [
            item
            for item in warehouse_rows
            if str(item.get("warehouse_state_code") or "").strip() not in _JOURNAL_HIDDEN_WAREHOUSE_STATES
        ]
    problem_snapshot_ids = problem_box_snapshot_ids(agency=client_agency)
    processing_reserve_groups, processing_reserve_totals = _processing_reserve_groups(client_agency)
    processing_in_progress_groups, processing_in_progress_totals = _processing_in_progress_groups(client_agency)
    shipping_reserve_groups, shipping_reserve_totals = _shipping_reserve_groups(client_agency)
    processing_request_groups = _processing_active_request_groups(client_agency) if not staff_view else []
    shipping_request_groups = _shipping_active_request_groups(client_agency) if not staff_view else []
    for item in storage_rows:
        agency_obj = item.get("agency")
        client_name = _agency_journal_label(agency_obj)
        state_code = str(item.get("warehouse_state_code") or "").strip().lower()
        is_fbs_stock = bool(item.get("is_fbs_stock"))
        box_code = (item.get("box_code") or "-").strip() or "-"
        pallet_code = (item.get("pallet_code") or "-").strip() or "-"
        is_processing_source_box = state_code in _JOURNAL_PROCESSING_SOURCE_BOX_STATES and box_code != "-"
        qty_value = int(item.get("qty") or 0)
        first_qty_value = int(item.get("_journal_first_qty") or qty_value)
        is_problem_box = int(item.get("id") or 0) in problem_snapshot_ids
        problem_qty = first_qty_value if is_problem_box else 0
        shipping_stage_qty = (
            first_qty_value
            if not is_problem_box and state_code in _JOURNAL_CLIENT_SHIPPING_STATES
            else 0
        )
        shipping_reserved_qty = int(item.get("shipping_reserved_qty") or 0)
        if is_processing_source_box:
            pallet_code = "-"
        rows.append(
            {
                "created_at": item.get("updated_at"),
                "source_order_id": item.get("source_order_id") or item.get("order_id"),
                "source_order_type": item.get("source_order_type") or item.get("order_type"),
                "order_id": item.get("order_id"),
                "order_type": item.get("order_type"),
                "snapshot_id": int(item.get("id") or 0),
                "container_id": int(item.get("container_id") or 0),
                "parent_container_id": int(item.get("parent_container_id") or 0),
                "location_id": int(item.get("location_id") or 0),
                "active_context_type": item.get("active_context_type"),
                "active_context_id": item.get("active_context_id"),
                "active_context_status": item.get("active_context_status"),
                "last_event_type": item.get("last_event_type"),
                "last_event_context_type": item.get("last_event_context_type"),
                "last_event_context_id": item.get("last_event_context_id"),
                "warehouse_state_code": item.get("warehouse_state_code"),
                "is_processing_source_box": is_processing_source_box,
                "order_type_code": order_type_codes.get(item.get("order_type"), (item.get("order_type") or "").upper()),
                "order_type_label": order_type_titles.get(item.get("order_type"), item.get("order_type")),
                "client_label": client_name or "-",
                "agency_id": int(item.get("agency_id") or 0),
                "sku_ref_id": int(item.get("sku_ref_id") or 0),
                "sku": (item.get("sku") or "-").strip() or "-",
                "barcode": (item.get("barcode") or "-").strip() or "-",
                "name": (item.get("name") or "-").strip() or "-",
                "size": (item.get("size") or "-").strip() or "-",
                "box_size": (item.get("box_size") or "").strip(),
                "box_weight": (item.get("box_weight") or "").strip(),
                "goods_type": (item.get("goods_type") or "-").strip() or "-",
                "qty": qty_value,
                "is_fbs_stock": is_fbs_stock,
                "storage_contour_code": "FBS" if is_fbs_stock else "FBO",
                "storage_contour_label": (
                    "FBS (не FBO)" if is_fbs_stock else "FBO / основной"
                ),
                "storage_contour_hint": (
                    "Остаток находится только в контуре FBS. "
                    "Строка показана здесь для единого складского поиска."
                    if is_fbs_stock
                    else "Остаток находится в основном складском контуре, не в FBS."
                ),
                "fbs_qty": int(item.get("fbs_qty") or 0),
                "fbs_available_qty": int(item.get("fbs_available_qty") or 0),
                "fbs_reserved_qty": int(item.get("fbs_reserved_qty") or 0),
                "stock_main_qty": int(item.get("stock_main_qty") or 0),
                "stock_processing_qty": int(item.get("stock_processing_qty") or 0),
                "stock_otg_qty": int(item.get("stock_otg_qty") or 0),
                "processing_reserved_qty": int(item.get("processing_reserved_qty") or 0),
                "processing_in_progress_qty": 0,
                "shipping_reserved_qty": shipping_reserved_qty,
                "shipping_stage_qty": shipping_stage_qty,
                "shipping_work_qty": max(shipping_reserved_qty, shipping_stage_qty),
                "problem_qty": problem_qty,
                "processing_request_qty": 0,
                "shipping_request_qty": 0,
                "available_qty": int(item.get("available_qty") or 0),
                "box_code": box_code,
                "pallet_code": pallet_code,
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
                "_journal_source_row_count": int(item.get("_journal_source_row_count") or 1),
            }
        )

    if not staff_view:
        grouped = {}
        for row in rows:
            key = (
                row.get("sku"),
                row.get("barcode"),
                row.get("name"),
                row.get("size"),
                row.get("goods_type"),
            )
            qty_value = _parse_qty_value(row.get("qty")) or 0
            existing = grouped.get(key)
            if existing:
                existing["qty"] += qty_value
                existing["processing_reserved_qty"] += int(row.get("processing_reserved_qty") or 0)
                existing["processing_in_progress_qty"] += int(row.get("processing_in_progress_qty") or 0)
                existing["shipping_reserved_qty"] += int(row.get("shipping_reserved_qty") or 0)
                existing["shipping_stage_qty"] += int(row.get("shipping_stage_qty") or 0)
                existing["shipping_work_qty"] += int(row.get("shipping_work_qty") or 0)
                existing["problem_qty"] += int(row.get("problem_qty") or 0)
                existing["processing_request_qty"] += int(row.get("processing_request_qty") or 0)
                existing["shipping_request_qty"] += int(row.get("shipping_request_qty") or 0)
                existing["available_qty"] += int(row.get("available_qty") or 0)
                if row.get("created_at") and row["created_at"] > existing.get("created_at"):
                    existing["created_at"] = row["created_at"]
            else:
                item = dict(row)
                item["qty"] = qty_value
                grouped[key] = item
                continue
            existing["stock_main_qty"] += int(row.get("stock_main_qty") or 0)
            existing["stock_processing_qty"] += int(row.get("stock_processing_qty") or 0)
            existing["stock_otg_qty"] += int(row.get("stock_otg_qty") or 0)
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
            for item in (
                list(processing_reserve_groups)
                + list(processing_in_progress_groups)
                + list(shipping_reserve_groups)
                + list(processing_request_groups)
                + list(shipping_request_groups)
            )
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
        client_name = _agency_journal_label(client_agency)
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
            qty_value = int(item.get("reserved_qty") or item.get("request_qty") or 0)
            if qty_value <= 0:
                return
            row = reserve_only_rows.get(key)
            if row is None:
                row = {
                    "created_at": item.get("updated_at_max") or timezone.localtime(),
                    "source_order_id": "-",
                    "source_order_type": "processing",
                    "order_id": "-",
                    "order_type": "processing",
                    "snapshot_id": 0,
                    "container_id": 0,
                    "parent_container_id": 0,
                    "location_id": 0,
                    "active_context_type": None,
                    "active_context_id": None,
                    "active_context_status": None,
                    "last_event_type": None,
                    "last_event_context_type": None,
                    "last_event_context_id": None,
                    "warehouse_state_code": "",
                    "order_type_code": order_type_codes.get("processing", "ОБР"),
                    "order_type_label": order_type_titles.get("processing", "Обработка"),
                    "client_label": client_name or "-",
                    "agency_id": int(client_agency.id or 0) if client_agency else 0,
                    "sku": sku_value or "-",
                    "barcode": (item.get("barcode") or "-").strip() or "-",
                    "name": name_by_sku.get(
                        sku_value.lower(),
                        (item.get("name") or "-").strip() or "-",
                    ),
                    "size": size_value or "-",
                    "goods_type": goods_raw or "-",
                    "qty": 0,
                    "is_fbs_stock": False,
                    "fbs_qty": 0,
                    "fbs_available_qty": 0,
                    "fbs_reserved_qty": 0,
                    "stock_main_qty": 0,
                    "stock_processing_qty": 0,
                    "stock_otg_qty": 0,
                    "processing_reserved_qty": 0,
                    "processing_in_progress_qty": 0,
                    "shipping_reserved_qty": 0,
                    "shipping_stage_qty": 0,
                    "shipping_work_qty": 0,
                    "problem_qty": 0,
                    "processing_request_qty": 0,
                    "shipping_request_qty": 0,
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
                row["shipping_work_qty"] += qty_value

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

        existing_base_keys = {
            _request_base_key(row.get("agency_id"), row.get("sku"), row.get("size"))
            for row in list(rows) + list(reserve_only_rows.values())
        }
        for item in list(processing_request_groups) + list(shipping_request_groups):
            base_key = _request_base_key(item.get("agency_id"), item.get("sku_code"), item.get("size"))
            if base_key in existing_base_keys:
                continue
            _upsert_reserve_only_row(item, source="request_placeholder")
            existing_base_keys.add(base_key)

        for key, row in reserve_only_rows.items():
            if key in present_keys:
                continue
            present_keys.add(key)
            rows.append(row)
        _allocate_client_request_groups(rows, processing_request_groups, field="processing_request_qty")
        _allocate_client_request_groups(rows, shipping_request_groups, field="shipping_request_qty")
    else:
        rows = _aggregate_inventory_journal_snapshot_rows(rows)
    canceled_shipping_context_ids = _canceled_shipping_context_ids(rows)
    for row in rows:
        row["order_display"] = _movement_order_display(
            row,
            canceled_shipping_context_ids=canceled_shipping_context_ids,
        )
        row["source_label"] = row["order_display"] if row["order_display"] != "-" else (row.get("order_type_code") or "-")
        row["location_short"] = _short_location_label(
            zone=row.get("zone"),
            row=row.get("row_no"),
            section=row.get("section_no"),
            tier=row.get("tier_no"),
            cell=row.get("cell_no"),
            location=row.get("location"),
        )
        row["is_processing_consumed"] = _journal_row_is_processing_consumed(row)
        row["state_label"] = ""
        row["state_note"] = ""
        if row["is_processing_consumed"]:
            row["stock_main_qty"] = 0
            row["stock_processing_qty"] = 0
            row["stock_otg_qty"] = 0
            row["available_qty"] = 0
            row["processing_reserved_qty"] = 0
            row["processing_in_progress_qty"] = 0
            row["shipping_reserved_qty"] = 0
            row["shipping_stage_qty"] = 0
            row["shipping_work_qty"] = 0
            row["problem_qty"] = 0
            row["processing_request_qty"] = 0
            row["shipping_request_qty"] = 0
            row["location"] = "Списали"
            row["location_short"] = "Списали"
        row["processing_work_qty"] = int(row.get("processing_reserved_qty") or 0) + int(
            row.get("processing_in_progress_qty") or 0
        )
        row["shipping_work_qty"] = max(
            int(row.get("shipping_work_qty") or 0),
            int(row.get("shipping_reserved_qty") or 0),
        )
        row["accounted_qty"] = (
            int(row.get("available_qty") or 0)
            + int(row.get("processing_work_qty") or 0)
            + int(row.get("shipping_work_qty") or 0)
            + int(row.get("problem_qty") or 0)
        )
        row["free_real_qty"] = max(
            int(row.get("accounted_qty") or 0)
            - int(row.get("processing_request_qty") or 0)
            - int(row.get("shipping_request_qty") or 0),
            0,
        )
        row["edit_lock_reason"] = _journal_edit_lock_reason(
            row,
            canceled_shipping_context_ids=canceled_shipping_context_ids,
        )
        if row.get("is_aggregated_snapshot_row"):
            row["edit_lock_reason"] = "Строка объединяет несколько складских единиц."
    if staff_view:
        rows = _expand_inventory_journal_status_rows(rows)
    else:
        rows = [
            row
            for row in rows
            if int(row.get("processing_work_qty") or 0) > 0
            or int(row.get("shipping_work_qty") or 0) > 0
            or int(row.get("problem_qty") or 0) > 0
            or int(row.get("processing_request_qty") or 0) > 0
            or int(row.get("shipping_request_qty") or 0) > 0
            or int(row.get("available_qty") or 0) > 0
        ]
    date_from_raw = (request.GET.get("date_from") or "").strip()
    date_to_raw = (request.GET.get("date_to") or "").strip()
    hide_processing_consumed = str(request.GET.get("hide_processing_consumed") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
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
    if hide_processing_consumed:
        rows = [row for row in rows if not row.get("is_processing_consumed")]
    filter_option_rows = list(rows)
    if (
        date_from
        or date_to
        or q
        or any(column_filters.values())
        or any(multi_column_filters.values())
    ):
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
                        row.get("storage_contour_code"),
                        row.get("storage_contour_label"),
                        row.get("sku"),
                        row.get("barcode"),
                        row.get("name"),
                        row.get("size"),
                        row.get("goods_type"),
                        row.get("qty"),
                        row.get("stock_main_qty"),
                        row.get("stock_processing_qty"),
                        row.get("stock_otg_qty"),
                        row.get("processing_work_qty"),
                        row.get("processing_reserved_qty"),
                        row.get("processing_in_progress_qty"),
                        row.get("shipping_reserved_qty"),
                        row.get("fbs_qty"),
                        row.get("fbs_available_qty"),
                        row.get("fbs_reserved_qty"),
                        row.get("available_qty"),
                        row.get("stock_status_label"),
                        row.get("stock_status_qty"),
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
            matched_multi_filters = True
            for filter_name, selected_keys in multi_column_filter_keys.items():
                if not selected_keys:
                    continue
                row_value = _journal_normalized_text(
                    _journal_filter_option_text(row, filter_name)
                )
                if row_value not in selected_keys:
                    matched_multi_filters = False
                    break
            if not matched_multi_filters:
                continue
            filtered_rows.append(row)
        rows = filtered_rows
    rows.sort(
        key=lambda item: _journal_sort_value(item, sort_key),
        reverse=(sort_dir == "desc"),
    )
    journal_summary = _build_inventory_journal_summary(rows)
    journal_focus = _build_inventory_journal_focus(rows, q)
    column_filter_options = _build_journal_filter_options(
        filter_option_rows,
        preferred_filters=column_filters,
    )
    for filter_name, selected_values in multi_column_filters.items():
        ordered_options: list[str] = []
        seen_options: set[str] = set()
        for value in [*selected_values, *column_filter_options.get(filter_name, [])]:
            normalized = _journal_normalized_text(value)
            if not normalized or normalized in seen_options:
                continue
            seen_options.add(normalized)
            ordered_options.append(value)
        column_filter_options[filter_name] = ordered_options
    journal_has_filters = bool(
        q
        or date_from_raw
        or date_to_raw
        or any(column_filters.values())
        or any(multi_column_filters.values())
        or hide_processing_consumed
    )
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
    manager_cabinet_view = role in _MANAGER_CABINET_ROLES
    page_obj = None
    page_links = []
    page_query = ""
    display_rows = rows
    if staff_view and not manager_cabinet_view:
        paginator = Paginator(rows, 100)
        page_obj = paginator.get_page(request.GET.get("page"))
        display_rows = list(page_obj.object_list)
        page_params = request.GET.copy()
        page_params.pop("page", None)
        page_query = page_params.urlencode()
        current_page = int(page_obj.number)
        last_page = int(paginator.num_pages)
        visible_pages = {
            1,
            last_page,
            *range(max(1, current_page - 2), min(last_page, current_page + 2) + 1),
        }
        previous_page = None
        for page_number in sorted(visible_pages):
            if previous_page is not None and page_number - previous_page > 1:
                page_links.append({"ellipsis": True})
            page_links.append(
                {
                    "number": page_number,
                    "current": page_number == current_page,
                    "ellipsis": False,
                }
            )
            previous_page = page_number
    for row in display_rows:
        row["barcode_lines"] = _journal_display_lines(row.get("barcode"))
        row["name_lines"] = _journal_display_lines(row.get("name"))
    if staff_view:
        missing_barcode_keys = {
            (
                int(row.get("agency_id") or 0),
                str(row.get("barcode") or "").strip(),
            )
            for row in display_rows
            if not int(row.get("sku_ref_id") or 0)
            and int(row.get("agency_id") or 0)
            and str(row.get("barcode") or "").strip() not in {"", "-"}
        }
        sku_id_by_client_barcode = (
            {
                (int(agency_id), str(value or "").strip()): int(sku_id)
                for agency_id, value, sku_id in SKUBarcode.objects.filter(
                    agency_id__in={key[0] for key in missing_barcode_keys},
                    value__in={key[1] for key in missing_barcode_keys},
                    sku__deleted=False,
                ).values_list("agency_id", "value", "sku_id")
                if (int(agency_id), str(value or "").strip()) in missing_barcode_keys
            }
            if missing_barcode_keys
            else {}
        )
        for row in display_rows:
            if not int(row.get("sku_ref_id") or 0):
                row["sku_ref_id"] = int(
                    sku_id_by_client_barcode.get(
                        (
                            int(row.get("agency_id") or 0),
                            str(row.get("barcode") or "").strip(),
                        )
                    )
                    or 0
                )

    if not staff_view:
        template_name = "client_cabinet/inventory_journal.html"
    elif manager_cabinet_view:
        template_name = "teammanager/inventory_journal.html"
    else:
        template_name = "sklad/inventory_journal.html"
    if manager_cabinet_view:
        inventory_home_url = "/team-manager/inventory/"
        inventory_cabinet_url = "/team-manager/"
    elif not staff_view:
        inventory_home_url = "/sklad/journal/"
        if client_agency:
            inventory_cabinet_url = f"/client/dashboard/lk/?client={client_agency.id}"
        else:
            inventory_cabinet_url = "/client/dashboard/lk/"
    else:
        inventory_home_url = "/sklad/journal/"
        inventory_cabinet_url = "/sklad/"
    problem_boxes = problem_box_rows(agency=client_agency) if staff_view else []
    problem_pallets = problem_pallet_rows(agency=client_agency) if staff_view else []
    missing_boxes = missing_box_rows(agency=client_agency, query=q) if staff_view else []
    return {
        "template_name": template_name,
        "context": {
            "rows": display_rows,
            "page_obj": page_obj,
            "page_links": page_links,
            "page_query": page_query,
            "client_agency": client_agency,
            "staff_view": staff_view,
            "q": q,
            "date_from": date_from_raw,
            "date_to": date_to_raw,
            "column_filters": column_filters,
            "column_filter_options": column_filter_options,
            "multi_column_filters": multi_column_filters,
            "journal_sort_key": sort_key,
            "journal_sort_dir": sort_dir,
            "journal_sort_urls": sort_urls,
            "journal_has_filters": journal_has_filters,
            "journal_reset_url": journal_reset_url,
            "journal_hide_processing_consumed": hide_processing_consumed,
            "journal_toggle_consumed_url": _build_journal_query_url(
                request,
                hide_processing_consumed=None if hide_processing_consumed else 1,
            ),
            "journal_summary": journal_summary,
            "journal_focus": journal_focus,
            "inventory_home_url": inventory_home_url,
            "inventory_cabinet_url": inventory_cabinet_url,
            "problem_boxes": problem_boxes,
            "problem_pallets": problem_pallets,
            "missing_boxes": missing_boxes,
            "daily_problem_control": (
                _daily_problem_control_context(
                    boxes=problem_boxes,
                    pallets=problem_pallets,
                    missing_boxes=missing_boxes,
                )
                if staff_view
                else None
            ),
            "label_settings": load_label_settings(),
        },
    }
