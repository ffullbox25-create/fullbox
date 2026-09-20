from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
import base64
import hashlib
import json
import logging
import re

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from audit.models import log_order_action, log_stock_move
from employees.access import resolve_cabinet_url
from employees.models import Employee
from fullbox.order_numbers import format_order_number, next_public_order_number
from reachtruck.models import MoveRequest, MoveTask
from reachtruck.services import create_shipping_pick_request
from reachtruck.services.claims import active_pallet_lock_codes
from sklad.models import WarehouseReserve, WarehouseStockSnapshot
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.dispatchable_stock import dispatchable_warehouse_state_codes
from sklad.services.warehouse_stock_rows import snapshot_stock_rows
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency, Market
from todo.models import Task

from .box_picking import BoxPickPlan, pick_boxes_for_quantity
from .box_splits import encode_partial_box_split, extract_partial_box_split
from .item_binding import (
    _positive_int,
    _catalog_barcode_alias_map,
    _catalog_item_identity,
    build_shipping_final_truth_snapshot,
    enforced_marketplace_item_binding_errors,
    enforced_ozon_request_binding_errors,
    marketplace_binding_mismatch_rows,
    should_enforce_marketplace_item_binding,
    shipping_final_truth_rows,
    shipping_supplement_fact_is_complete,
    validate_marketplace_item_binding,
    validate_ozon_request_binding,
)
from .models import ShippingOrder, ShippingOrderItem


logger = logging.getLogger(__name__)
_BOX_CODES_COMMENT_RE = re.compile(r"короба:\s*([^;]+)", re.IGNORECASE)
_SOURCE_BOX_CODES_COMMENT_RE = re.compile(r"Исходные\s+короба:\s*[^;]+", re.IGNORECASE)
_PARTIAL_BOX_SPLIT_TOKEN_RE = re.compile(r"split_box:b64:[A-Za-z0-9_-]+={0,2}")
_BOX_COUNT_COMMENT_RE = re.compile(r"коробов:\s*(\d+)", re.IGNORECASE)
_OZON_API_COMMENT_TITLE = "Данные Ozon API:"
_SUBMISSION_DATE_DELIVERY_TYPES = {
    ShippingOrder.DELIVERY_PICKUP,
    ShippingOrder.DELIVERY_COURIER,
}
_WAREHOUSE_EDIT_LOCK_MESSAGE = (
    "Заявка уже передана в складскую работу. "
    "Редактирование из ЛК клиента или менеджера недоступно. "
    "Обратитесь к складу: отменить или вернуть такую заявку может только склад."
)
_SHIPPING_QUANTITY_MISMATCH_WARNING_PREFIX = "Расхождение отгрузки по ШК и количеству:"
_STRICT_OZON_GM_INTAKE_START_AT = datetime.fromisoformat(
    "2026-08-11T06:00:00+00:00"
)


class RoutingStateSyncError(ValueError):
    """Маршрутизация не синхронизирована; вызывающая транзакция должна откатиться."""


def _shipping_form_save_error_message(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, NameError) and "exclude_shipping_order_id" in message:
        return _WAREHOUSE_EDIT_LOCK_MESSAGE
    return f"Не удалось сохранить заявку: {message}"


def _visible_shipping_integrity_warnings(
    warnings: list[str],
    discrepancy_context: dict | None,
) -> list[str]:
    if not isinstance(discrepancy_context, dict):
        return list(warnings or [])
    if not (
        discrepancy_context.get("status") == "resolved"
        and discrepancy_context.get("decision") == "supplement"
    ):
        return list(warnings or [])
    return [
        warning
        for warning in list(warnings or [])
        if not str(warning or "").startswith(_SHIPPING_QUANTITY_MISMATCH_WARNING_PREFIX)
    ]


def _staff_client_queryset_for_role(role: str | None):
    qs = Agency.objects.filter(archived=False)
    if role == "manager":
        from accountant.selectors import manager_visible_agencies

        qs = manager_visible_agencies(qs)
    return qs.order_by("agn_name")


def _local_date(value=None):
    current = value or timezone.localtime()
    if timezone.is_naive(current):
        current = timezone.make_aware(current, timezone.get_current_timezone())
    return timezone.localtime(current).date()


def _shipping_print_agent_context(*, request, order_pk: int | None = None) -> dict:
    scanner_agents: list[dict] = []
    preferred_agent_id = ""
    preferred_agent_locked = False
    try:
        from agent.models import AgentContext, DeviceAgent

        now = timezone.now()
        online_threshold = now - timedelta(seconds=30)
        if getattr(request.user, "is_authenticated", False):
            context_qs = AgentContext.objects.filter(
                user=request.user,
                active=True,
                expires_at__gt=now,
            )
            if order_pk is not None:
                context_qs = context_qs.filter(order_id=order_pk)
            session_key_text = str(getattr(request.session, "session_key", "") or "").strip()
            active_context = None
            if session_key_text:
                active_context = (
                    context_qs.filter(session_key=session_key_text)
                    .order_by("-last_seen", "-updated_at")
                    .first()
                )
                if active_context and active_context.agent_id:
                    preferred_agent_locked = True
            if not active_context and context_qs.count() == 1:
                active_context = context_qs.order_by("-last_seen", "-updated_at").first()
                if active_context and active_context.agent_id:
                    preferred_agent_locked = True
            if active_context and active_context.agent_id:
                preferred_agent_id = str(active_context.agent_id).strip()
        for agent in DeviceAgent.objects.all().order_by("-last_seen", "-updated_at"):
            is_online = bool(agent.last_seen and agent.last_seen >= online_threshold)
            scanner_agents.append(
                {
                    "agent_id": agent.agent_id,
                    "title": agent.name or agent.host or agent.agent_id,
                    "status": "онлайн" if is_online else "нет связи",
                    "is_online": is_online,
                    "last_seen": agent.last_seen.isoformat() if agent.last_seen else "",
                }
            )
    except Exception:
        scanner_agents = []
        preferred_agent_id = ""
        preferred_agent_locked = False
    return {
        "scanner_agents": scanner_agents,
        "preferred_agent_id": preferred_agent_id,
        "preferred_agent_locked": preferred_agent_locked,
    }


def next_shipping_number() -> str:
    numbers = ShippingOrder.objects.values_list("number", flat=True)
    return next_public_order_number("shipping", numbers)


def _key(sku_code: str, size: str, goods_type: str, barcode: str = "") -> tuple[str, ...]:
    barcode_value = (barcode or "").strip().lower()
    if barcode_value:
        return ("barcode", barcode_value)
    return (
        "sku",
        (sku_code or "").strip().lower(),
        (size or "").strip().lower(),
        StockAvailabilityService.normalize_goods_type(goods_type),
    )


def _shipping_reserve_blockers_by_item(
    order: ShippingOrder,
    items: list[ShippingOrderItem],
    *,
    reserve_rows=None,
) -> dict[tuple[str, ...], list[tuple[str, int]]]:
    """Return active foreign shipping reserves in one batch, for diagnostics only."""
    if reserve_rows is None:
        barcodes = {
            str(item.barcode or "").strip()
            for item in items
            if str(item.barcode or "").strip()
        }
        sku_codes = {
            str(item.sku_code or "").strip()
            for item in items
            if str(item.sku_code or "").strip()
        }
        lookup = Q()
        if barcodes:
            lookup |= Q(barcode__in=barcodes)
        if sku_codes:
            lookup |= Q(sku_code__in=sku_codes)
        if not barcodes and not sku_codes:
            return {}
        active_statuses = {
            WarehouseReserve.STATUS_ACTIVE,
            WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
            WarehouseReserve.STATUS_ALLOCATED,
            WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
        }
        reserve_rows = list(
            WarehouseReserve.objects.filter(
                lookup,
                agency=order.agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                status__in=active_statuses,
            )
            .exclude(context_type="shipping", context_id=order.number)
            .values(
                "sku_code",
                "size",
                "goods_type",
                "barcode",
                "context_id",
                "qty_reserved",
                "qty_satisfied",
            )
        )

    by_item: defaultdict[tuple[str, ...], Counter] = defaultdict(Counter)
    for reserve in reserve_rows or []:
        if isinstance(reserve, dict):
            value = reserve
        else:
            value = {
                "sku_code": getattr(reserve, "sku_code", ""),
                "size": getattr(reserve, "size", ""),
                "goods_type": getattr(reserve, "goods_type", ""),
                "barcode": getattr(reserve, "barcode", ""),
                "context_id": getattr(reserve, "context_id", ""),
                "qty_reserved": getattr(reserve, "qty_reserved", 0),
                "qty_satisfied": getattr(reserve, "qty_satisfied", 0),
            }
        remaining_qty = max(
            int(value.get("qty_reserved") or 0) - int(value.get("qty_satisfied") or 0),
            0,
        )
        context_id = str(value.get("context_id") or "").strip()
        if remaining_qty <= 0 or not context_id:
            continue
        item_key = _key(
            value.get("sku_code") or "",
            value.get("size") or "",
            value.get("goods_type") or "",
            value.get("barcode") or "",
        )
        by_item[item_key][format_order_number("shipping", context_id)] += remaining_qty
    return {
        item_key: sorted(counter.items())
        for item_key, counter in by_item.items()
    }


def _format_shipping_reserve_shortage(
    item: ShippingOrderItem,
    *,
    needed: int,
    available: int,
    blockers: list[tuple[str, int]] | None = None,
) -> str:
    identity = f"артикул {item.sku_code or '-'}"
    barcode = str(item.barcode or "").strip()
    if barcode:
        identity += f", ШК {barcode}"
    shortage = max(int(needed or 0) - max(int(available or 0), 0), 0)
    message = (
        f"{identity}: требуется {int(needed or 0)} шт., "
        f"доступно {max(int(available or 0), 0)} шт., не хватает {shortage} шт."
    )
    if blockers:
        reserved = ", ".join(
            f"{order_number} — {qty} шт."
            for order_number, qty in blockers
        )
        return f"{message} Зарезервировано в заявках: {reserved}."
    return f"{message} В активных заявках этот товар не найден."


def _assert_pool_reserve_capacity_locked(order: ShippingOrder, items: list[ShippingOrderItem]) -> None:
    """Повторная проверка pool-резерва под row-lock, без изменения остатков."""
    barcodes = {
        str(item.barcode or "").strip()
        for item in items
        if str(item.barcode or "").strip()
    }
    sku_codes = {
        str(item.sku_code or "").strip()
        for item in items
        if str(item.sku_code or "").strip()
    }
    if not barcodes and not sku_codes:
        return

    lookup = Q()
    if barcodes:
        lookup |= Q(barcode__in=barcodes)
    if sku_codes:
        lookup |= Q(sku_code__in=sku_codes)

    snapshot_rows = (
        WarehouseStockSnapshot.objects.select_for_update()
        .filter(agency=order.agency, is_archived=False)
        .filter(lookup)
        .order_by("id")
    )
    available: dict[tuple[str, ...], int] = defaultdict(int)
    for row in snapshot_rows:
        available[_key(row.sku_code, row.size, row.goods_type, row.barcode)] += int(
            row.available_qty or 0
        ) + int(row.shipping_reserved_qty or 0)

    active_statuses = {
        WarehouseReserve.STATUS_ACTIVE,
        WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
        WarehouseReserve.STATUS_ALLOCATED,
        WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
    }
    reserve_rows = list(
        WarehouseReserve.objects.select_for_update()
        .filter(agency=order.agency, reserve_type=WarehouseReserve.TYPE_SHIPPING, status__in=active_statuses)
        .exclude(context_type="shipping", context_id=order.number)
        .filter(lookup)
        .order_by("id")
    )
    for reserve in reserve_rows:
        available[_key(reserve.sku_code, reserve.size, reserve.goods_type, reserve.barcode)] -= int(
            reserve.qty_reserved or 0
        )

    blockers_by_item = _shipping_reserve_blockers_by_item(
        order,
        items,
        reserve_rows=reserve_rows,
    )
    shortages: list[str] = []
    for item in items:
        needed = int(item.qty_requested or 0)
        if needed <= 0:
            continue
        key = _key(item.sku_code, item.size, item.goods_type, item.barcode)
        locked_available = int(available.get(key, 0))
        if locked_available < needed:
            shortages.append(
                _format_shipping_reserve_shortage(
                    item,
                    needed=needed,
                    available=locked_available,
                    blockers=blockers_by_item.get(key),
                )
            )
            continue
        available[key] = locked_available - needed
    if shortages:
        raise ValidationError(
            "Недостаточно товара для резерва после проверки активных резервов: "
            + " ".join(shortages)
        )


def _matching_pool_shipping_reserve_exists_locked(
    order: ShippingOrder,
    items: list[ShippingOrderItem],
) -> bool:
    """Return True when the order already owns the complete pool reserve."""
    reserves = list(
        WarehouseReserve.objects.select_for_update()
        .filter(
            agency=order.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=order.number,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        .prefetch_related("events")
        .order_by("id")
    )
    if not reserves:
        return False
    if any(
        not reserve.events.filter(payload__reserve_scope="pool").exists()
        for reserve in reserves
    ):
        return False

    requested: dict[tuple[str, ...], int] = defaultdict(int)
    for item in items:
        qty = int(item.qty_requested or 0)
        if qty > 0:
            requested[_key(item.sku_code, item.size, item.goods_type, item.barcode)] += qty

    reserved: dict[tuple[str, ...], int] = defaultdict(int)
    for reserve in reserves:
        reserved[_key(reserve.sku_code, reserve.size, reserve.goods_type, reserve.barcode)] += int(
            reserve.qty_reserved or 0
        )
    if dict(reserved) != dict(requested):
        return False

    requested_hints: dict[tuple[str, ...], list[tuple[int, tuple[str, ...]]]] = defaultdict(list)
    for item in items:
        qty = max(int(item.qty_requested or 0), 0)
        if qty <= 0:
            continue
        codes = tuple(code.casefold() for code in _box_codes_from_item_comment(item.comment))
        requested_hints[_key(item.sku_code, item.size, item.goods_type, item.barcode)].append((qty, codes))

    reserved_hints: dict[tuple[str, ...], list[tuple[int, tuple[str, ...]]]] = defaultdict(list)
    for reserve in reserves:
        codes: list[str] = []
        for event in reserve.events.all():
            payload = event.payload if isinstance(event.payload, dict) else {}
            if str(payload.get("reserve_scope") or "").strip() != "pool":
                continue
            codes = [
                str(code or "").strip().casefold()
                for code in payload.get("box_codes") or []
                if str(code or "").strip()
            ]
            break
        reserved_hints[_key(reserve.sku_code, reserve.size, reserve.goods_type, reserve.barcode)].append(
            (max(int(reserve.qty_reserved or 0), 0), tuple(codes))
        )
    if not any(
        codes
        for hints in reserved_hints.values()
        for _qty, codes in hints
    ):
        return True
    normalize_hints = lambda values: sorted(values, key=lambda value: (value[0], value[1]))
    return {
        key: normalize_hints(value)
        for key, value in requested_hints.items()
    } == {
        key: normalize_hints(value)
        for key, value in reserved_hints.items()
    }


def _box_codes_from_item_comment(comment: str | None) -> list[str]:
    match = _BOX_CODES_COMMENT_RE.search(str(comment or ""))
    if not match:
        return []
    codes: list[str] = []
    seen: set[str] = set()
    for raw_code in match.group(1).split(","):
        code = str(raw_code or "").strip()
        normalized = code.lower()
        if not code or normalized in seen:
            continue
        seen.add(normalized)
        codes.append(code)
    return codes


def _comment_with_reselected_box_codes(
    comment: str | None,
    box_codes: list[str],
) -> str:
    """Replace box hints while preserving the rest of the client request."""
    original = str(comment or "").strip()
    codes: list[str] = []
    seen: set[str] = set()
    for raw_code in box_codes or []:
        code = str(raw_code or "").strip()
        normalized = code.casefold()
        if not code or normalized in seen:
            continue
        seen.add(normalized)
        codes.append(code)
    if not codes:
        return original

    joined_codes = ", ".join(codes)
    split_meta = extract_partial_box_split(original)
    if split_meta:
        split_meta = dict(split_meta)
        split_meta["source_box_codes"] = codes
        marker = encode_partial_box_split(split_meta)
        if _PARTIAL_BOX_SPLIT_TOKEN_RE.search(original):
            updated = _PARTIAL_BOX_SPLIT_TOKEN_RE.sub(marker, original, count=1)
        else:
            updated = f"{original}; {marker}" if original else marker
        source_label = f"Исходные короба: {joined_codes}"
        if _SOURCE_BOX_CODES_COMMENT_RE.search(updated):
            return _SOURCE_BOX_CODES_COMMENT_RE.sub(source_label, updated, count=1)
        return f"{source_label}; {updated}" if updated else source_label

    box_label = f"короба: {joined_codes}"
    if _BOX_CODES_COMMENT_RE.search(original):
        return _BOX_CODES_COMMENT_RE.sub(box_label, original, count=1)
    return f"{original}; {box_label}" if original else box_label


def _reselect_selected_boxes_locked(
    order: ShippingOrder,
    items: list[ShippingOrderItem],
) -> bool:
    """Refresh stale Ozon box hints from reservation-aware availability.

    The availability service excludes reservations of every other shipping
    request. This helper only updates selection hints on the current request;
    the existing warehouse write-path creates the reserve afterwards.
    """
    from .ozon_supplies import allocate_reserve_box_codes

    stock_rows = list(
        StockAvailabilityService.stock_rows_with_availability(
            agency=order.agency,
            require_box=True,
            exclude_shipping_order_id=order.number,
        )
    )
    requested_items = [
        {
            "item_id": item.pk,
            "item_key": _key(item.sku_code, item.size, item.goods_type, item.barcode),
            "qty": max(int(item.qty_requested or 0), 0),
            "preferred_box_codes": _box_codes_from_item_comment(item.comment),
        }
        for item in items
        if int(item.qty_requested or 0) > 0
    ]
    reserve_rows = [
        {
            "item_key": _key(
                row.get("sku"),
                row.get("size"),
                row.get("goods_type"),
                row.get("barcode"),
            ),
            "box_code": row.get("box_code") or row.get("container_code"),
            "available_qty": max(int(row.get("available_qty") or 0), 0),
            "qty": max(int(row.get("qty") or 0), 0),
        }
        for row in stock_rows
    ]
    allocation = allocate_reserve_box_codes(requested_items, reserve_rows)
    if not allocation.get("ok"):
        return False

    changed = False
    selected_by_item = allocation.get("box_codes_by_item") or {}
    for item in items:
        selected_codes = selected_by_item.get(item.pk) or []
        if not selected_codes:
            continue
        updated_comment = _comment_with_reselected_box_codes(item.comment, selected_codes)
        if updated_comment == str(item.comment or ""):
            continue
        item.comment = updated_comment
        item.save(update_fields=["comment", "updated_at"])
        changed = True
    return changed


def _assert_selected_box_reserve_capacity_locked(
    order: ShippingOrder,
    items: list[ShippingOrderItem],
) -> None:
    selected_items = [
        (item, _box_codes_from_item_comment(item.comment))
        for item in items
        if int(item.qty_requested or 0) > 0
    ]
    selected_items = [(item, codes) for item, codes in selected_items if codes]
    if not selected_items:
        return

    box_availability: dict[tuple[str, tuple[str, ...]], int] = defaultdict(int)
    for row in StockAvailabilityService.stock_rows_with_availability(
        agency=order.agency,
        require_box=True,
        exclude_shipping_order_id=order.number,
    ):
        box_code = str(row.get("box_code") or row.get("container_code") or "").strip().casefold()
        if not box_code:
            continue
        item_key = _key(
            row.get("sku"),
            row.get("size"),
            row.get("goods_type"),
            row.get("barcode"),
        )
        box_availability[(box_code, item_key)] += max(int(row.get("available_qty") or 0), 0)

    shortages: list[str] = []
    for item, box_codes in selected_items:
        item_key = _key(item.sku_code, item.size, item.goods_type, item.barcode)
        remaining_qty = max(int(item.qty_requested or 0), 0)
        selected_available = 0
        for raw_code in box_codes:
            box_key = str(raw_code or "").strip().casefold()
            lookup = (box_key, item_key)
            available_qty = max(int(box_availability.get(lookup, 0)), 0)
            selected_available += available_qty
            applied_qty = min(available_qty, remaining_qty)
            box_availability[lookup] = available_qty - applied_qty
            remaining_qty -= applied_qty
            if remaining_qty <= 0:
                break
        if remaining_qty > 0:
            shortages.append(
                f"{item.sku_code}/{item.size or '-'}: в выбранных коробах требуется "
                f"{item.qty_requested}, доступно {selected_available}"
            )
    if shortages:
        raise ValidationError(
            "Недостаточно товара в выбранных коробах после проверки активных резервов: "
            + "; ".join(shortages)
        )


def shipping_selectable_warehouse_state_codes(*, include_reserved: bool = False) -> tuple[str, ...]:
    # The state says whether stock is released; the physical zone only tells
    # the reachtruck where to collect it. AvailabilityService still removes all
    # quantities held by processing, shipping, FBS, or another active reserve.
    codes = list(dispatchable_warehouse_state_codes(include_receiving=True))
    if include_reserved:
        codes.append(WarehouseStateCode.RESERVED_FOR_SHIPPING.value)
    return tuple(codes)


def _first_active_employee_by_roles(*roles: str) -> Employee | None:
    for role in [role for role in roles if role]:
        employee = (
            Employee.objects.filter(role=role, is_active=True)
            .order_by("full_name")
            .first()
        )
        if employee:
            return employee
    return None


def _client_manager(agency) -> Employee | None:
    try:
        from client_cabinet.other_requests import resolve_client_manager

        manager = resolve_client_manager(agency)
        if manager:
            return manager
    except Exception:
        pass
    return _first_active_employee_by_roles("manager", "head_manager")


def _claimed_manager_for_order(order: ShippingOrder) -> Employee | None:
    if not order.pk:
        return None
    task = (
        Task.objects.filter(
            route=f"/shipping/{order.pk}/",
            title=f"Заявка на отгрузку №{order.number}",
            assigned_to__role__in=["manager", "head_manager"],
            assigned_to__is_active=True,
        )
        .select_related("assigned_to")
        .order_by("-updated_at", "-id")
        .first()
    )
    return task.assigned_to if task else None


def _least_loaded_employee_by_role(role: str) -> Employee | None:
    employees = list(
        Employee.objects.filter(role=role, is_active=True).order_by("full_name", "id")
    )
    if not employees:
        return None
    employee_ids = [employee.id for employee in employees]
    active_counts = dict(
        Task.objects.filter(assigned_to_id__in=employee_ids)
        .exclude(status="done")
        .values("assigned_to_id")
        .annotate(total=Count("id"))
        .values_list("assigned_to_id", "total")
    )
    return min(
        employees,
        key=lambda employee: (
            active_counts.get(employee.id, 0),
            str(employee.full_name or ""),
            employee.id,
        ),
    )


def _manager_due_date(submitted_at=None):
    base_dt = submitted_at or timezone.localtime()
    if timezone.is_naive(base_dt):
        base_dt = timezone.make_aware(base_dt, timezone.get_current_timezone())
    base_dt = timezone.localtime(base_dt)
    return base_dt + timedelta(hours=24)


def _storekeeper_due_date(order: ShippingOrder):
    target_date = order.planned_ship_date or order.slot_date
    if target_date:
        target_time = order.slot_time or time(23, 59, 59)
        due_date = datetime.combine(target_date, target_time)
        if timezone.is_naive(due_date):
            due_date = timezone.make_aware(due_date, timezone.get_current_timezone())
        return due_date
    eta_at = getattr(order, "eta_at", None)
    if eta_at:
        if timezone.is_naive(eta_at):
            eta_at = timezone.make_aware(eta_at, timezone.get_current_timezone())
        return timezone.localtime(eta_at)
    return timezone.localtime()


def ensure_manager_review_task(order: ShippingOrder, user=None, *, submitted_at=None) -> Task | None:
    if not order.pk or not order.agency_id:
        return None
    route = f"/shipping/{order.pk}/"
    title = f"Заявка на отгрузку №{order.number}"
    description = f"Клиент: {order.agency.agn_name or order.agency.inn or order.agency.id}"
    due_date = _manager_due_date(submitted_at)
    existing = (
        Task.objects.filter(route=route, title=title)
        .filter(Q(assigned_to__isnull=True) | Q(assigned_to__role__in=["manager", "head_manager"]))
        .order_by("-created_at")
        .first()
    )
    if existing:
        existing.description = description
        update_fields = ["description", "status", "updated_at"]
        if existing.status == "done" or not existing.due_date:
            existing.due_date = due_date
            update_fields.append("due_date")
        if existing.status == "done":
            existing.status = "backlog"
        existing.save(update_fields=update_fields)
        return existing
    return Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=None,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=due_date,
    )


def close_manager_review_task(order: ShippingOrder) -> None:
    if not order.pk:
        return
    title = f"Заявка на отгрузку №{order.number}"
    (
        Task.objects.filter(route=f"/shipping/{order.pk}/", title=title)
        .filter(
            Q(assigned_to__isnull=True)
            | Q(assigned_to__role__in=["manager", "head_manager"])
        )
        .exclude(status="done")
        .update(status="done")
    )


def close_manager_cancel_appeal_task(order: ShippingOrder) -> None:
    if not order.pk:
        return
    Task.objects.filter(
        route=f"/shipping/{order.pk}/",
        assigned_to__role__in=["manager", "head_manager"],
        title__startswith="Согласуйте отмену заявки",
    ).exclude(status="done").update(status="done")


def ensure_warehouse_cancel_review_task(order: ShippingOrder, user=None, *, reason: str = "") -> Task | None:
    if not order.pk or not order.agency_id:
        return None
    storekeeper = _first_active_employee_by_roles("storekeeper")
    if not storekeeper:
        return None
    route = f"/shipping/{order.pk}/"
    title = f"Подтвердите отмену отгрузки №{order.number}"
    description = f"Клиент: {order.agency.agn_name or order.agency.inn or order.agency.id}"
    if str(reason or "").strip():
        description += f"\nПричина отмены: {str(reason).strip()}"
    existing = Task.objects.filter(route=route, assigned_to=storekeeper, title=title).exclude(status="done").first()
    if existing:
        existing.description = description
        existing.due_date = timezone.localtime()
        existing.save(update_fields=["description", "due_date", "updated_at"])
        return existing
    return Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=storekeeper,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=timezone.localtime(),
    )


def close_warehouse_cancel_review_task(order: ShippingOrder) -> None:
    if not order.pk:
        return
    Task.objects.filter(
        route=f"/shipping/{order.pk}/",
        assigned_to__role="storekeeper",
        title__startswith="Подтвердите отмену отгрузки",
    ).exclude(status="done").update(status="done")


def ensure_storekeeper_task(order: ShippingOrder, user=None) -> Task | None:
    if not order.pk or not order.agency_id:
        return None
    storekeeper = _first_active_employee_by_roles("storekeeper")
    if not storekeeper:
        return None
    route = f"/shipping/{order.pk}/"
    title = f"Заявка на отгрузку №{order.number}"
    description = f"Клиент: {order.agency.agn_name or order.agency.inn or order.agency.id}"
    due_date = _storekeeper_due_date(order)
    existing = (
        Task.objects.filter(route=route, assigned_to__role="storekeeper")
        .order_by("-created_at")
        .first()
    )
    if existing:
        existing.title = title
        existing.description = description
        existing.assigned_to = storekeeper
        existing.due_date = due_date
        if existing.status == "done":
            existing.status = "backlog"
        existing.save(
            update_fields=["title", "description", "assigned_to", "due_date", "status", "updated_at"]
        )
        return existing
    task = Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=storekeeper,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=due_date,
    )
    return task


def close_storekeeper_task(order: ShippingOrder) -> None:
    if not order.pk:
        return
    Task.objects.filter(
        route=f"/shipping/{order.pk}/",
        assigned_to__role="storekeeper",
    ).exclude(status="done").update(status="done")


def mark_storekeeper_task_in_progress(order: ShippingOrder) -> None:
    if not order.pk:
        return
    (
        Task.objects.filter(
            route=f"/shipping/{order.pk}/",
            assigned_to__role="storekeeper",
        )
        .exclude(status="done")
        .update(status="in_progress")
    )


@transaction.atomic
def ensure_logistician_task(order: ShippingOrder, user=None) -> Task | None:
    if not order.pk or not order.agency_id:
        return None
    order = (
        ShippingOrder.objects.select_for_update(of=("self",))
        .select_related("agency")
        .get(pk=order.pk)
    )
    if order.delivery_type in {
        ShippingOrder.DELIVERY_PICKUP,
        ShippingOrder.DELIVERY_TRANSFER,
    }:
        close_logistician_task(order)
        return None
    route = f"/shipping/{order.pk}/"
    title = f"Заявка на отгрузку №{order.number}"
    description = (
        f"Клиент: {order.agency.agn_name or order.agency.inn or order.agency.id}. "
        "Склад завершил подготовку, требуется погрузка и акт отгрузки."
    )
    due_date = timezone.localtime()
    existing = (
        Task.objects.select_for_update()
        .filter(route=route, assigned_to__role="logistician")
        .order_by("-created_at")
        .first()
    )
    logistician = None
    if (
        existing
        and existing.status != "done"
        and existing.assigned_to_id
        and existing.assigned_to.is_active
    ):
        logistician = existing.assigned_to
    if logistician is None:
        logistician = _least_loaded_employee_by_role("logistician")
    if not logistician:
        return None
    if existing:
        existing.title = title
        existing.description = description
        existing.assigned_to = logistician
        existing.due_date = due_date
        if existing.status == "done":
            existing.status = "backlog"
        existing.save(
            update_fields=["title", "description", "assigned_to", "due_date", "status", "updated_at"]
        )
        _mark_routing_ready_or_raise(order)
        return existing
    task = Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=logistician,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=due_date,
    )
    _mark_routing_ready_or_raise(order)
    return task


def _mark_routing_ready_or_raise(order: ShippingOrder) -> None:
    try:
        from logistics.routing_services import mark_ready_for_routing

        mark_ready_for_routing(order)
    except Exception as exc:
        logger.exception(
            "Failed to mark shipping order ready for routing",
            extra={
                "shipping_order_id": order.pk,
                "shipping_operation": "mark_ready_for_routing",
            },
        )
        raise RoutingStateSyncError(
            "Не удалось передать заявку в маршрутизацию. "
            "Изменения не сохранены; повторите операцию или обратитесь к администратору."
        ) from exc


def close_logistician_task(order: ShippingOrder) -> None:
    if not order.pk:
        return
    Task.objects.filter(
        route=f"/shipping/{order.pk}/",
        assigned_to__role="logistician",
    ).exclude(status="done").update(status="done")


def ensure_shipping_act_logistician_task(order: ShippingOrder, user=None) -> Task | None:
    if not order.pk or not order.agency_id:
        return None
    route = f"/shipping/{order.pk}/act/"
    title = f"Подписать акт отгрузки №{order.number}"
    description = (
        f"Клиент: {order.agency.agn_name or order.agency.inn or order.agency.id}. "
        "Склад подтвердил фактическую погрузку по сканам. "
        "Проверьте заявленное и отгруженное количество и подпишите акт."
    )
    due_date = timezone.localtime()
    existing = (
        Task.objects.filter(route=route, assigned_to__role="logistician")
        .order_by("-created_at")
        .first()
    )
    logistician = None
    if (
        existing
        and existing.assigned_to_id
        and existing.assigned_to.is_active
    ):
        logistician = existing.assigned_to
    if logistician is None:
        logistician = _least_loaded_employee_by_role("logistician")
    if not logistician:
        return None
    if existing:
        existing.title = title
        existing.description = description
        existing.assigned_to = logistician
        existing.due_date = due_date
        if existing.status == "done":
            existing.status = "backlog"
        existing.save(
            update_fields=["title", "description", "assigned_to", "due_date", "status", "updated_at"]
        )
        return existing
    return Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=logistician,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=due_date,
    )


def close_shipping_act_logistician_task(order: ShippingOrder) -> None:
    if not order.pk:
        return
    (
        Task.objects.filter(
            route=f"/shipping/{order.pk}/act/",
            title=f"Подписать акт отгрузки №{order.number}",
            assigned_to__role="logistician",
        )
        .exclude(status="done")
        .update(status="done")
    )


def ensure_shipping_act_manager_task(order: ShippingOrder, user=None, *, observer: Employee | None = None) -> Task | None:
    if not order.pk or not order.agency_id:
        return None
    manager = _claimed_manager_for_order(order)
    route = f"/shipping/{order.pk}/act/"
    title = f"Подписать акт отгрузки №{order.number}"
    description = (
        f"Клиент: {order.agency.agn_name or order.agency.inn or order.agency.id}. "
        "Логист подписал акт отгрузки, требуется подпись менеджера."
    )
    due_date = timezone.localtime()
    existing = (
        Task.objects.filter(route=route)
        .filter(Q(assigned_to__isnull=True) | Q(assigned_to__role__in=["manager", "head_manager"]))
        .order_by("-created_at")
        .first()
    )
    if existing:
        existing.title = title
        existing.description = description
        if manager is not None:
            existing.assigned_to = manager
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
        assigned_to=manager,
        observer=observer,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=due_date,
    )


def close_shipping_act_manager_task(order: ShippingOrder) -> None:
    if not order.pk:
        return
    (
        Task.objects.filter(
            route=f"/shipping/{order.pk}/act/",
            title=f"Подписать акт отгрузки №{order.number}",
        )
        .filter(
            Q(assigned_to__isnull=True)
            | Q(assigned_to__role__in=["manager", "head_manager"])
        )
        .exclude(status="done")
        .update(status="done")
    )


def shipping_available_items(
    order: ShippingOrder,
    *,
    exclude_order: ShippingOrder | None = None,
) -> list[dict]:
    exclude_shipping_order_id = exclude_order.number if exclude_order is not None else None
    return StockAvailabilityService.inventory_items_for_agency(
        order.agency,
        exclude_shipping_order_id=exclude_shipping_order_id,
        warehouse_state_codes=shipping_selectable_warehouse_state_codes(
            include_reserved=exclude_order is not None,
        ),
    )


def order_payload(order: ShippingOrder, *, extra: dict | None = None) -> dict:
    items = [
        {
            "id": item.id,
            "sku_code": item.sku_code,
            "name": item.name,
            "size": item.size,
            "barcode": item.barcode,
            "goods_type": item.goods_type,
            "qty_requested": item.qty_requested,
            "qty_reserved": item.qty_reserved,
            "qty_shipped": item.qty_shipped,
            "comment": item.comment,
        }
        for item in order.items.order_by("id")
    ]
    payload = {
        "shipping_state": order.status,
        "number": order.number,
        "shipping_barcode": order.shipping_barcode,
        "supply_number": getattr(order, "supply_number", ""),
        "delivery_type": order.delivery_type,
        "marketplace": order.marketplace.name if order.marketplace_id else "",
        "marketplace_id": order.marketplace_id,
        "slot_date": order.slot_date.isoformat() if order.slot_date else "",
        "destination_warehouse": order.destination_warehouse,
        "supply_type": order.supply_type,
        "destination_address": order.destination_address,
        "planned_ship_date": order.planned_ship_date.isoformat() if order.planned_ship_date else "",
        "vehicle_type": order.vehicle_type,
        "wb_supply_barcode": order.wb_supply_barcode,
        "wb_transit_warehouse": bool(order.wb_transit_warehouse),
        "transit_address": order.transit_address,
        "eta_at": order.eta_at.isoformat() if order.eta_at else "",
        "expected_boxes": int(order.expected_boxes or 0),
        "place_type": order.place_type,
        "vehicle_number": order.vehicle_number,
        "driver_phone": order.driver_phone,
        "comment": order.comment,
        "items": items,
    }
    if extra:
        payload.update(extra)
    return payload


def _log_order(order: ShippingOrder, *, action: str, user, description: str, extra: dict | None = None) -> None:
    def _looks_like_question_noise(value) -> bool:
        text = str(value or "").strip()
        if text.count("?") < 6:
            return False
        meaningful = [char for char in text if not char.isspace()]
        if not meaningful:
            return False
        cyrillic = sum(0x0400 <= ord(char) <= 0x04FF for char in text)
        letters = sum(char.isalpha() for char in text)
        question_ratio = text.count("?") / max(len(meaningful), 1)
        return cyrillic == 0 and (question_ratio >= 0.35 or letters == 0)

    def _fallback_description() -> str:
        status = str((extra or {}).get("shipping_state") or order.status or "").strip().lower()
        return {
            ShippingOrder.STATUS_SHIPPED: "Заявка отгружена",
            ShippingOrder.STATUS_PARTIAL: "Заявка отгружена частично",
            ShippingOrder.STATUS_PACKED: "Кладовщик подготовил заявку к отгрузке",
            ShippingOrder.STATUS_PICKING: "Созданы задания ричтраку на отбор в OTG",
            ShippingOrder.STATUS_RESERVED: "Резерв подтвержден",
            ShippingOrder.STATUS_STOREKEEPER_ACCEPTED: "Кладовщик принял заявку в работу",
            ShippingOrder.STATUS_SUBMITTED: "Заявка ожидает подтверждения менеджера",
            ShippingOrder.STATUS_DRAFT: "Заявка отредактирована менеджером",
            ShippingOrder.STATUS_CANCELED: "Заявка отменена",
        }.get(status, "Статус заявки обновлен")

    clean_description = str(description or "").strip()
    if _looks_like_question_noise(clean_description):
        clean_description = _fallback_description()
    log_order_action(
        action=action,
        order_id=order.number,
        order_type="shipping",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=order.agency,
        description=clean_description,
        payload=order_payload(order, extra=extra),
    )


def _as_clean_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_as_clean_list(item))
        return list(dict.fromkeys(result))
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return _as_clean_list(parsed)
    return list(dict.fromkeys(part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()))


def _normalize_ozon_gm_cargoes(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    result: list[dict] = []
    seen: set[str] = set()
    # A combined client request can contain up to ten Ozon supplies.  Keep the
    # full marketplace snapshot for audit/display instead of silently cutting
    # it at the old single-supply limit of 50 GM labels.
    for source in value:
        if not isinstance(source, dict):
            continue
        gm_barcode = str(source.get("gm_barcode") or "").strip()
        gm_key = gm_barcode.lower()
        if not gm_barcode or gm_key in seen:
            continue
        seen.add(gm_key)
        items: list[dict] = []
        for source_item in (source.get("items") or []):
            if not isinstance(source_item, dict):
                continue
            offer_id = str(source_item.get("offer_id") or "").strip()
            barcode = str(source_item.get("barcode") or "").strip()
            try:
                quantity = max(int(source_item.get("quantity") or 0), 0)
            except (TypeError, ValueError):
                quantity = 0
            if not offer_id and not barcode:
                continue
            items.append(
                {
                    "offer_id": offer_id,
                    "barcode": barcode,
                    "name": str(source_item.get("name") or "").strip(),
                    "quantity": quantity,
                    "ozon_sku": str(source_item.get("ozon_sku") or "").strip(),
                }
            )
        result.append(
            {
                "gm_barcode": gm_barcode,
                "cargo_id": str(source.get("cargo_id") or "").strip(),
                "supply_id": str(source.get("supply_id") or "").strip(),
                "bundle_id": str(source.get("bundle_id") or "").strip(),
                "transport_cargo_id": str(source.get("transport_cargo_id") or "").strip(),
                "ozon_supply_number": str(source.get("ozon_supply_number") or "").strip(),
                "ozon_order_id": str(source.get("ozon_order_id") or "").strip(),
                "items": items,
            }
        )
    return result


def _build_ozon_api_snapshot_payload(meta: dict) -> dict:
    """Return an immutable, normalized Ozon snapshot for the order audit.

    The snapshot is informational only. It must never be used as a Fullbox
    box binding or be passed to the warehouse reserve write-path implicitly.
    """
    meta = meta if isinstance(meta, dict) else {}
    source_payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
    gm_cargoes = _normalize_ozon_gm_cargoes(meta.get("gm_cargoes"))
    try:
        boxes_total = max(int(meta.get("boxes_total") or 0), 0)
    except (TypeError, ValueError):
        boxes_total = 0
    supply_id = re.sub(
        r"\s+",
        "",
        str(meta.get("order_number") or meta.get("supply_number") or "").strip(),
    )
    supply_numbers = _as_clean_list(meta.get("supply_numbers"))
    order_ids = _as_clean_list(meta.get("order_ids"))
    if not supply_numbers and supply_id:
        supply_numbers = [supply_id]
    complete = bool(
        boxes_total > 0
        and len(gm_cargoes) == boxes_total
        and all(
            cargo.get("gm_barcode")
            and cargo.get("items")
            and all(
                item.get("barcode") and int(item.get("quantity") or 0) > 0
                for item in cargo.get("items") or []
            )
            for cargo in gm_cargoes
        )
    )
    canonical = {
        "supply_id": supply_id,
        "supply_numbers": supply_numbers,
        "order_ids": order_ids,
        "boxes_total": boxes_total,
        "gm_cargoes": gm_cargoes,
    }
    snapshot_hash = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    fetched_at = str(
        source_payload.get("fetched_at")
        or source_payload.get("snapshot_fetched_at")
        or ""
    ).strip() or timezone.now().isoformat()
    return {
        "ozon_snapshot_schema_version": 1,
        "ozon_snapshot_source": "ozon_api",
        "ozon_snapshot_fetched_at": fetched_at,
        "ozon_supply_id_normalized": supply_id,
        "ozon_supply_numbers": supply_numbers,
        "ozon_order_ids": order_ids,
        "ozon_supply_count": len(supply_numbers),
        "ozon_snapshot_complete": complete,
        "ozon_snapshot_hash": snapshot_hash,
        "ozon_boxes_total": boxes_total,
        "ozon_composition": list(meta.get("composition") or []),
        "ozon_destination_warehouses": list(meta.get("destinations") or []),
        "gm_cargoes": gm_cargoes,
    }


def _ozon_snapshot_supply_values(payload: dict) -> list[str]:
    if not isinstance(payload, dict):
        return []
    values: list[str] = []
    for value in (
        payload.get("ozon_supply_id_normalized"),
        payload.get("ozon_order_number"),
        payload.get("supply_number"),
        payload.get("ozon_supply_numbers"),
    ):
        values.extend(_as_clean_list(value))
    return list(
        dict.fromkeys(
            re.sub(r"\s+", "", str(value or "").strip())
            for value in values
            if str(value or "").strip()
        )
    )


def _ozon_gm_snapshot_matches_selected_rows(
    payload: dict,
    selected_rows: list[dict],
) -> bool:
    """Return True only for a complete GM snapshot matching the draft exactly."""
    if not isinstance(payload, dict) or not payload.get("ozon_snapshot_complete"):
        return False
    cargoes = _normalize_ozon_gm_cargoes(payload.get("gm_cargoes"))
    if not cargoes or any(not cargo.get("items") for cargo in cargoes):
        return False

    selected_totals: Counter[str] = Counter()
    aliases: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for row in selected_rows or []:
        if not isinstance(row, dict):
            continue
        barcode = str(row.get("barcode") or "").strip()
        sku_code = str(row.get("sku_code") or row.get("sku") or "").strip()
        quantity = _positive_int(
            row.get("qty_requested")
            or row.get("qty")
            or row.get("quantity")
        )
        if quantity <= 0 or not (barcode or sku_code):
            continue
        # Ozon identifies a product by offer_id, while one Fullbox SKU may have
        # several valid stock barcodes. Keep those rows in one product bucket.
        identity = f"sku:{sku_code.casefold()}" if sku_code else f"barcode:{barcode.casefold()}"
        selected_totals[identity] += quantity
        if barcode:
            aliases[("barcode", barcode.casefold())].add(identity)
        if sku_code:
            aliases[("sku", sku_code.casefold())].add(identity)
    if not selected_totals:
        return False

    gm_totals: Counter[str] = Counter()
    for cargo in cargoes:
        for item in cargo.get("items") or []:
            if not isinstance(item, dict):
                return False
            barcode = str(item.get("barcode") or "").strip()
            offer_id = str(item.get("offer_id") or "").strip()
            quantity = _positive_int(item.get("quantity"))
            candidates: set[str] = set()
            if barcode:
                candidates.update(aliases.get(("barcode", barcode.casefold())) or set())
            if offer_id:
                candidates.update(aliases.get(("sku", offer_id.casefold())) or set())
            if quantity <= 0 or len(candidates) != 1:
                return False
            gm_totals[next(iter(candidates))] += quantity
    return gm_totals == selected_totals


def _saved_ozon_api_intake_meta(
    order: ShippingOrder,
    *,
    selected_rows: list[dict] | None = None,
    prefer_exact_match: bool = False,
) -> dict:
    """Read the appropriate immutable Ozon API snapshot saved for the shipment."""
    try:
        from audit.models import OrderAuditEntry

        entries = OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id=str(getattr(order, "number", "") or ""),
        ).order_by("id")[:100]
        matched_payloads: list[dict] = []
        for entry in entries:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if not (
                payload.get("ozon_snapshot_source") == "ozon_api"
                or payload.get("is_ozon_api_supply")
                or payload.get("mass_ozon_submit")
            ):
                continue
            matched_payloads.append(payload)
        if matched_payloads:
            exact_payload = None
            if prefer_exact_match and selected_rows:
                order_supply_values = {
                    re.sub(r"\s+", "", value)
                    for source in _ozon_supply_identity_values(order)
                    for value in _as_clean_list(source)
                    if value
                }
                for candidate in reversed(matched_payloads):
                    candidate_supply_values = set(_ozon_snapshot_supply_values(candidate))
                    if (
                        order_supply_values
                        and candidate_supply_values
                        and order_supply_values.isdisjoint(candidate_supply_values)
                    ):
                        continue
                    if _ozon_gm_snapshot_matches_selected_rows(candidate, selected_rows):
                        exact_payload = candidate
                        break
            payload = exact_payload or next(
                (
                    row
                    for row in reversed(matched_payloads)
                    if row.get("ozon_snapshot_complete")
                    and (
                        row.get("act")
                        in {
                            "shipping_ozon_gm_intake_refresh",
                            "shipping_ozon_gm_reconcile_after_item_removal",
                        }
                        or row.get("ozon_gm_reconciled_after_item_removal")
                    )
                ),
                None,
            )
            if payload is None:
                payload = next(
                    (
                        row
                        for row in matched_payloads
                        if row.get("ozon_snapshot_complete")
                        and row.get("gm_cargoes")
                        and int(row.get("ozon_boxes_total") or 0) > 0
                    ),
                    None,
                )
            if payload is None:
                payload = next(
                    (row for row in matched_payloads if row.get("gm_cargoes")),
                    matched_payloads[0],
                )
            return {
                "payload": payload,
                "order_number": (
                    payload.get("ozon_supply_id_normalized")
                    or payload.get("ozon_order_number")
                    or payload.get("supply_number")
                    or ""
                ),
                "supply_numbers": payload.get("ozon_supply_numbers") or [],
                "order_ids": payload.get("ozon_order_ids") or [],
                "boxes_total": payload.get("ozon_boxes_total") or 0,
                "composition": payload.get("ozon_composition") or [],
                "destinations": payload.get("ozon_destination_warehouses") or [],
                "gm_cargoes": payload.get("gm_cargoes") or [],
                "_selected_rows_exact_match": exact_payload is not None,
            }
    except Exception:
        logger.exception(
            "Failed to read immutable Ozon intake snapshot for shipping order %s",
            getattr(order, "pk", None),
        )

    # An API comment without an audit snapshot is itself unsafe: the warehouse
    # must not guess GM composition from display text.
    if _OZON_API_COMMENT_TITLE in str(getattr(order, "comment", "") or ""):
        return {"payload": {"snapshot_source": "ozon_api_comment"}}
    return {}


def _strict_ozon_gm_intake_errors(
    order: ShippingOrder,
    *,
    ozon_api_meta: dict | None = None,
) -> list[str]:
    """Validate immutable Ozon GM input without enabling automatic GM binding."""
    if not _is_current_ozon_shipping(order):
        return []
    created_at = getattr(order, "created_at", None)
    if created_at is not None and timezone.is_naive(created_at):
        created_at = timezone.make_aware(created_at, timezone.get_default_timezone())
    if created_at is not None and created_at < _STRICT_OZON_GM_INTAKE_START_AT:
        return []

    meta = ozon_api_meta if isinstance(ozon_api_meta, dict) else {}
    if not (isinstance(meta.get("payload"), dict) and meta.get("payload")):
        meta = _saved_ozon_api_intake_meta(order)
    if not meta:
        return []

    snapshot = _build_ozon_api_snapshot_payload(meta)
    cargoes = list(snapshot.get("gm_cargoes") or [])
    boxes_total = int(snapshot.get("ozon_boxes_total") or 0)
    errors: list[str] = []
    if not snapshot.get("ozon_snapshot_complete"):
        errors.append(
            "Ozon не передал полный состав всех ШК ГМ. Обновите данные поставки: "
            "склад не будет угадывать состав или автоматически делить короб."
        )

    binding = validate_ozon_request_binding(
        order,
        gm_cargoes=cargoes,
        expected_boxes=order.expected_boxes,
        ozon_boxes_total=boxes_total,
        compare_source_box_count=False,
    )
    errors.extend(str(error or "").strip() for error in binding.get("errors") or [])
    return list(dict.fromkeys(error for error in errors if error))


def _raise_for_strict_ozon_gm_intake(
    order: ShippingOrder,
    *,
    ozon_api_meta: dict | None = None,
) -> None:
    errors = _strict_ozon_gm_intake_errors(order, ozon_api_meta=ozon_api_meta)
    if errors:
        raise ValidationError(
            [
                "Заявка Ozon не передана складу: проверьте ШК ГМ, товарные ШК, "
                "количество и число грузовых мест.",
                *errors,
            ]
        )


def refresh_strict_ozon_gm_intake(
    order: ShippingOrder,
    user=None,
) -> list[str]:
    """Refresh a delayed Ozon GM composition before warehouse acceptance."""
    current_errors = _strict_ozon_gm_intake_errors(order)
    if not current_errors:
        return []

    meta = _saved_ozon_api_intake_meta(order)
    source_cargoes = list(meta.get("gm_cargoes") or [])

    try:
        from .ozon_supplies import (
            _normalize_ozon_client_id,
            enrich_gm_cargoes_with_items,
            fetch_ozon_gm_cargoes,
            resolve_ozon_credential,
        )

        credential, credential_error = resolve_ozon_credential(order.agency)
        if credential_error or credential is None:
            return current_errors
        client_id = _normalize_ozon_client_id(credential.client_id or "")
        api_key = str(credential.market_key or "").strip()
        saved_supply_numbers = _as_clean_list(meta.get("supply_numbers"))
        saved_order_number = str(meta.get("order_number") or "").strip()
        order_supply_number = str(order.supply_number or "").strip()
        saved_matches_order = (
            not order_supply_number
            or order_supply_number in saved_supply_numbers
            or saved_order_number == order_supply_number
        )
        if order_supply_number and not saved_matches_order:
            # A previously saved immutable intake snapshot can belong to a
            # different Ozon supply after the client replaces the selected
            # supply.  Never merge such cargoes with the current supply.
            source_cargoes = []
            supply_numbers = [order_supply_number]
        else:
            supply_numbers = saved_supply_numbers
            if order_supply_number and not supply_numbers:
                supply_numbers = [order_supply_number]

        fresh_cargoes_loaded = False
        if supply_numbers:
            fresh_cargoes, _fresh_error = fetch_ozon_gm_cargoes(
                client_id,
                api_key,
                supply_numbers,
            )
            if fresh_cargoes:
                source_cargoes = fresh_cargoes
                fresh_cargoes_loaded = True
        if not source_cargoes:
            return current_errors
        enriched = enrich_gm_cargoes_with_items(
            client_id,
            api_key,
            source_cargoes,
            pause_seconds=0.5,
        )
        normalized = _normalize_ozon_gm_cargoes(enriched)
        snapshot = _build_ozon_api_snapshot_payload(
            {
                "order_number": order.supply_number or meta.get("order_number"),
                "supply_numbers": supply_numbers,
                "order_ids": (meta.get("order_ids") or []) if saved_matches_order else [],
                "boxes_total": (
                    len(normalized)
                    if fresh_cargoes_loaded
                    else meta.get("boxes_total") or len(normalized)
                ),
                "gm_cargoes": normalized,
            }
        )
        if not snapshot.get("ozon_snapshot_complete"):
            return current_errors

        binding = validate_ozon_request_binding(
            order,
            gm_cargoes=normalized,
            expected_boxes=order.expected_boxes,
            ozon_boxes_total=int(snapshot.get("ozon_boxes_total") or 0),
            compare_source_box_count=False,
        )
        if binding.get("errors"):
            return current_errors

        log_order_action(
            action="update",
            order_id=order.number,
            order_type="shipping",
            user=user,
            agency=order.agency,
            description="Состав ШК ГМ Ozon обновлен перед принятием заявки в работу",
            payload=order_payload(
                order,
                extra={
                    **snapshot,
                    "act": "shipping_ozon_gm_intake_refresh",
                    "gm_barcodes": [row["gm_barcode"] for row in normalized],
                },
            ),
        )
    except Exception:
        logger.exception(
            "Could not refresh Ozon GM intake before shipping acceptance for order %s",
            getattr(order, "pk", None),
        )
        return current_errors

    return _strict_ozon_gm_intake_errors(order)


def _refresh_ozon_gm_composition_for_loose_packing(
    order: ShippingOrder,
    *,
    user=None,
) -> bool:
    """Refresh delayed cargo composition before opening the loose packing UI.

    Ozon can publish cargo barcodes before the bundle endpoint starts returning
    their item lines.  The warehouse must still validate the total item
    composition, but the number of source Fullbox boxes is allowed to differ
    from the final number of Ozon cargoes because partial source boxes can be
    consolidated during packing.
    """
    if not _is_current_ozon_shipping(order):
        return False

    meta = _saved_ozon_api_intake_meta(order)
    source_cargoes = [
        dict(row)
        for row in meta.get("gm_cargoes") or []
        if isinstance(row, dict)
    ]
    if not source_cargoes or all(row.get("items") for row in source_cargoes):
        return False

    try:
        from .ozon_supplies import (
            _normalize_ozon_client_id,
            enrich_gm_cargoes_with_items,
            resolve_ozon_credential,
        )

        credential, credential_error = resolve_ozon_credential(order.agency)
        if credential_error or credential is None:
            return False
        enriched = enrich_gm_cargoes_with_items(
            _normalize_ozon_client_id(credential.client_id or ""),
            str(credential.market_key or "").strip(),
            source_cargoes,
            pause_seconds=0.5,
        )
        normalized = _normalize_ozon_gm_cargoes(enriched)
        if len(normalized) != len(source_cargoes) or any(
            not row.get("items") for row in normalized
        ):
            return False

        boxes_total = _positive_int(meta.get("boxes_total")) or len(normalized)
        binding = validate_ozon_request_binding(
            order,
            gm_cargoes=normalized,
            # Fullbox source boxes may be consolidated into fewer Ozon cargoes.
            expected_boxes=boxes_total,
            ozon_boxes_total=boxes_total,
        )
        if binding.get("errors"):
            return False
        snapshot = _build_ozon_api_snapshot_payload(
            {
                "order_number": meta.get("order_number") or order.supply_number,
                "supply_numbers": meta.get("supply_numbers") or [order.supply_number],
                "order_ids": meta.get("order_ids") or [],
                "boxes_total": boxes_total,
                "gm_cargoes": normalized,
            }
        )
        if not snapshot.get("ozon_snapshot_complete"):
            return False
        log_order_action(
            action="update",
            order_id=order.number,
            order_type="shipping",
            user=user,
            agency=order.agency,
            description="Состав ШК ГМ Ozon обновлен перед упаковкой товара без короба",
            payload=order_payload(
                order,
                extra={
                    **snapshot,
                    "act": "shipping_ozon_gm_loose_packing_refresh",
                    "gm_barcodes": [row["gm_barcode"] for row in normalized],
                },
            ),
        )
    except Exception:
        logger.exception(
            "Could not refresh Ozon GM composition before loose packing for order %s",
            getattr(order, "pk", None),
        )
        return False
    return True


def _ozon_supply_meta_from_request(request) -> dict:
    raw_payload = str(request.POST.get("ozon_supply_payload_json") or "").strip()
    payload = {}
    if raw_payload:
        try:
            parsed = json.loads(raw_payload)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            payload = parsed
    form_data = payload.get("form") if isinstance(payload.get("form"), dict) else {}
    gm_barcodes = _as_clean_list(payload.get("gm_barcodes"))
    if not gm_barcodes:
        gm_barcodes = _as_clean_list(request.POST.get("ozon_gm_barcodes"))
    ozon_gm_comment = str(
        form_data.get("ozon_gm_comment")
        or request.POST.get("ozon_gm_comment")
        or ""
    ).strip()
    if not gm_barcodes and ozon_gm_comment:
        gm_barcodes = _as_clean_list(ozon_gm_comment.split(":", 1)[1] if ":" in ozon_gm_comment else ozon_gm_comment)
    try:
        boxes_total = int(payload.get("ozon_boxes_total") or request.POST.get("ozon_boxes_total") or 0)
    except (TypeError, ValueError):
        boxes_total = 0
    composition = payload.get("composition") if isinstance(payload.get("composition"), list) else []
    destinations = payload.get("destination_warehouses") if isinstance(payload.get("destination_warehouses"), list) else []
    gm_cargoes = _normalize_ozon_gm_cargoes(payload.get("gm_cargoes"))
    supply_numbers: list[str] = []
    order_ids: list[str] = []
    for source in payload.get("supplies") or []:
        if not isinstance(source, dict):
            continue
        source_form = source.get("form") if isinstance(source.get("form"), dict) else {}
        supply_number = str(
            source.get("order_number")
            or source.get("supply_number")
            or source_form.get("supply_number")
            or ""
        ).strip()
        order_id = str(source.get("order_id") or "").strip()
        if supply_number and supply_number not in supply_numbers:
            supply_numbers.append(supply_number)
        if order_id and order_id not in order_ids:
            order_ids.append(order_id)
    top_supply_number = str(
        payload.get("order_number")
        or form_data.get("supply_number")
        or request.POST.get("supply_number")
        or ""
    ).strip()
    top_order_id = str(payload.get("order_id") or "").strip()
    if not supply_numbers and top_supply_number:
        supply_numbers.append(top_supply_number)
    if not order_ids and top_order_id:
        order_ids.append(top_order_id)
    return {
        "payload": payload,
        "order_id": top_order_id,
        "order_ids": order_ids,
        "order_number": top_supply_number,
        "shipping_barcode": str(form_data.get("shipping_barcode") or request.POST.get("shipping_barcode") or "").strip(),
        "supply_number": str(form_data.get("supply_number") or request.POST.get("supply_number") or "").strip(),
        "supply_numbers": supply_numbers,
        "is_combined": bool(payload.get("is_combined_ozon_supply") or len(supply_numbers) > 1),
        "ozon_gm_comment": ozon_gm_comment,
        "gm_barcodes": gm_barcodes,
        "gm_cargoes": gm_cargoes,
        "boxes_total": boxes_total or len(gm_barcodes),
        "composition": composition,
        "destinations": [str(item or "").strip() for item in destinations if str(item or "").strip()],
    }


def _ozon_api_summary_from_request(request) -> dict:
    """Restore the visible Ozon block after a rejected create/update POST.

    A failed submission previously rebuilt this block only from a saved model.
    New requests have no model yet, so the already verified Ozon payload and all
    counters disappeared and the form misleadingly displayed zero items.
    """
    meta = _ozon_supply_meta_from_request(request)
    payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
    if not payload:
        return {"show": False}

    form_data = payload.get("form") if isinstance(payload.get("form"), dict) else {}
    composition = payload.get("composition")
    if not isinstance(composition, list):
        composition = payload.get("items") if isinstance(payload.get("items"), list) else []
    item_rows: list[dict] = []
    for item in composition:
        if not isinstance(item, dict):
            continue
        quantity = max(_positive_int(item.get("quantity")), 0)
        if quantity <= 0:
            continue
        item_rows.append(
            {
                "sku_code": str(item.get("offer_id") or "—").strip() or "—",
                "name": str(item.get("name") or "—").strip() or "—",
                "barcode": str(item.get("barcode") or "—").strip() or "—",
                "qty_requested": quantity,
                "boxes": max(_positive_int(item.get("applied_boxes")), 0),
                "gm_barcodes": ", ".join(_as_clean_list(item.get("ozon_gm_barcodes"))),
            }
        )

    gm_barcodes = list(meta.get("gm_barcodes") or [])
    gm_cargoes = list(meta.get("gm_cargoes") or [])
    destinations = list(meta.get("destinations") or [])
    destination_warehouse = str(
        form_data.get("destination_warehouse")
        or (destinations[0] if destinations else "")
        or request.POST.get("destination_warehouse")
        or ""
    ).strip()
    transit_address = str(
        form_data.get("transit_address")
        or request.POST.get("transit_address")
        or ""
    ).strip()
    supply_number = str(
        meta.get("supply_number")
        or meta.get("order_number")
        or request.POST.get("supply_number")
        or ""
    ).strip()
    shipping_barcode = str(
        meta.get("shipping_barcode")
        or request.POST.get("shipping_barcode")
        or ""
    ).strip()
    boxes_total = max(_positive_int(meta.get("boxes_total")), 0)
    gm_comment = str(meta.get("ozon_gm_comment") or "").strip()
    return {
        "show": True,
        "supply_number": supply_number,
        "shipping_barcode": shipping_barcode,
        "boxes_total": boxes_total,
        "destination_warehouse": destination_warehouse,
        "destination_warehouses": destinations,
        "destination_missing": not bool(destination_warehouse or destinations),
        "transit_address": transit_address,
        "gm_barcodes": gm_barcodes,
        "gm_cargoes": gm_cargoes,
        "gm_matched_count": sum(
            1 for row in gm_cargoes if str(row.get("assignment_status") or "") == "matched"
        ),
        "gm_unassigned_count": sum(
            1 for row in gm_cargoes if str(row.get("assignment_status") or "") == "unassigned"
        ),
        "gm_barcodes_label": ", ".join(gm_barcodes),
        "gm_comment": gm_comment,
        "item_rows": item_rows,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "payload": payload,
    }


def _ozon_api_audit_extra(meta: dict | None) -> dict:
    if not meta:
        return {}
    extra = {
        **_build_ozon_api_snapshot_payload(meta),
        "ozon_order_id": meta.get("order_id"),
        "ozon_order_ids": meta.get("order_ids"),
        "ozon_order_number": meta.get("order_number"),
        "ozon_supply_numbers": meta.get("supply_numbers"),
        "ozon_supply_count": len(meta.get("supply_numbers") or []),
        "ozon_boxes_total": meta.get("boxes_total"),
        "ozon_gm_comment": meta.get("ozon_gm_comment"),
        "gm_barcodes": meta.get("gm_barcodes"),
        "gm_cargoes": meta.get("gm_cargoes"),
        "is_ozon_api_supply": bool(meta.get("payload")),
    }
    if meta.get("ozon_gm_reconciled_after_item_removal"):
        extra["ozon_gm_reconciled_after_item_removal"] = True
        extra["removed_gm_barcodes"] = list(meta.get("removed_gm_barcodes") or [])
    return extra


def _reconcile_ozon_gm_meta_with_selected_rows(
    *,
    agency,
    meta: dict,
    selected_rows: list[dict],
) -> tuple[dict, list[str]]:
    """Remove GM cargoes that belong only to products removed from the request.

    A GM cargo is indivisible.  We filter it only when its exact item
    composition is known and none of its items remains selected.  Mixed or
    quantity-ambiguous changes are blocked instead of guessing which GM label
    should survive.
    """
    result = dict(meta or {})
    cargoes = _normalize_ozon_gm_cargoes(result.get("gm_cargoes"))
    if not cargoes:
        return result, []

    if any(not cargo.get("items") for cargo in cargoes):
        try:
            from .ozon_supplies import (
                _normalize_ozon_client_id,
                enrich_gm_cargoes_with_items,
                resolve_ozon_credential,
            )

            credential, credential_error = resolve_ozon_credential(agency)
            if credential_error or credential is None:
                return result, [
                    "Ozon ещё не передал точный состав ШК ГМ. "
                    "Удаление товара не сохранено: невозможно однозначно удалить связанное грузоместо."
                ]
            cargoes = _normalize_ozon_gm_cargoes(
                enrich_gm_cargoes_with_items(
                    _normalize_ozon_client_id(credential.client_id or ""),
                    str(credential.market_key or "").strip(),
                    cargoes,
                    pause_seconds=0.5,
                    max_fetch=0,  # Form submission reuses the explicit preload step.
                )
            )
        except Exception:
            logger.exception("Could not load Ozon GM composition while editing a shipping request")
            return result, [
                "Не удалось проверить состав ШК ГМ Ozon. "
                "Изменение состава заявки не сохранено."
            ]
        if not cargoes or any(not cargo.get("items") for cargo in cargoes):
            return result, [
                "Ozon ещё не передал точный состав всех ШК ГМ. "
                "Выберите поставку кнопкой «Выбрать заявки Ozon» и дождитесь проверки коробов, затем повторите отправку."
            ]

    selected_totals: Counter[str] = Counter()
    selected_labels: dict[str, str] = {}
    selected_barcodes: defaultdict[str, dict[str, str]] = defaultdict(dict)
    aliases: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for index, row in enumerate(selected_rows or []):
        if not isinstance(row, dict):
            continue
        barcode = str(row.get("barcode") or "").strip()
        sku_code = str(row.get("sku_code") or row.get("sku") or "").strip()
        quantity = _positive_int(
            row.get("qty_requested")
            or row.get("qty")
            or row.get("quantity")
        )
        if quantity <= 0 or not (barcode or sku_code):
            continue
        # A selected SKU can legitimately be split over several stock barcode
        # variants. Compare Ozon GM quantities by the exact offer/SKU first.
        identity = f"sku:{sku_code.casefold()}" if sku_code else f"barcode:{barcode.casefold()}"
        selected_totals[identity] += quantity
        selected_labels.setdefault(identity, sku_code or barcode or f"позиция {index + 1}")
        if barcode:
            selected_barcodes[identity].setdefault(barcode.casefold(), barcode)
            aliases[("barcode", barcode.casefold())].add(identity)
        if sku_code:
            aliases[("sku", sku_code.casefold())].add(identity)

    kept_cargoes: list[dict] = []
    kept_totals: Counter[str] = Counter()
    errors: list[str] = []
    for cargo in cargoes:
        gm_barcode = str(cargo.get("gm_barcode") or "").strip() or "—"
        matched_items: list[tuple[str, int]] = []
        removed_items: list[str] = []
        for item in cargo.get("items") or []:
            if not isinstance(item, dict):
                continue
            barcode = str(item.get("barcode") or "").strip()
            offer_id = str(item.get("offer_id") or "").strip()
            quantity = _positive_int(item.get("quantity"))
            candidates: set[str] = set()
            if barcode:
                candidates.update(aliases.get(("barcode", barcode.casefold())) or set())
            if offer_id:
                candidates.update(aliases.get(("sku", offer_id.casefold())) or set())
            if len(candidates) > 1:
                errors.append(
                    f"ШК ГМ {gm_barcode} нельзя однозначно связать с выбранной позицией. "
                    "Изменение не сохранено."
                )
                continue
            if not candidates:
                removed_items.append(barcode or offer_id or "неизвестный товар")
                continue
            matched_items.append((next(iter(candidates)), quantity))

        if matched_items and removed_items:
            errors.append(
                f"ШК ГМ {gm_barcode} содержит одновременно оставленный и удалённый товар. "
                "Грузоместо нельзя удалить частично."
            )
            continue
        if not matched_items:
            # Every product in this cargo was removed from the Fullbox request.
            continue
        kept_cargoes.append(cargo)
        for identity, quantity in matched_items:
            kept_totals[identity] += quantity

    for identity in sorted(set(selected_totals) | set(kept_totals)):
        selected_qty = int(selected_totals.get(identity) or 0)
        gm_qty = int(kept_totals.get(identity) or 0)
        if selected_qty != gm_qty:
            errors.append(
                f"После удаления товара состав ШК ГМ не совпадает по позиции "
                f"{selected_labels.get(identity, identity)}: в заявке {selected_qty} шт., "
                f"в оставшихся грузоместах Ozon {gm_qty} шт."
            )
    if errors:
        return result, list(dict.fromkeys(errors))

    # Ozon can expose a catalog barcode in the supply composition and a
    # different, non-empty barcode for the same offer inside a GM label.  Once
    # the offer_id has matched exactly and all quantities agree, keep one
    # canonical Fullbox barcode for the final strict intake check.  Never fill
    # a missing GM barcode: the strict validator must continue to block it.
    aligned_cargoes: list[dict] = []
    for cargo in kept_cargoes:
        aligned_cargo = dict(cargo)
        aligned_items: list[dict] = []
        for item in cargo.get("items") or []:
            if not isinstance(item, dict):
                continue
            aligned_item = dict(item)
            source_barcode = str(item.get("barcode") or "").strip()
            offer_id = str(item.get("offer_id") or "").strip()
            candidates: set[str] = set()
            if source_barcode:
                candidates.update(
                    aliases.get(("barcode", source_barcode.casefold())) or set()
                )
            if offer_id:
                candidates.update(aliases.get(("sku", offer_id.casefold())) or set())
            if source_barcode and len(candidates) == 1:
                identity = next(iter(candidates))
                barcode_variants = selected_barcodes.get(identity) or {}
                canonical_barcode = (
                    next(iter(barcode_variants.values()))
                    if len(barcode_variants) == 1
                    else ""
                )
                if canonical_barcode and source_barcode.casefold() != canonical_barcode.casefold():
                    aligned_item["barcode"] = canonical_barcode
            aligned_items.append(aligned_item)
        aligned_cargo["items"] = aligned_items
        aligned_cargoes.append(aligned_cargo)
    kept_cargoes = aligned_cargoes

    original_gm_barcodes = [
        str(cargo.get("gm_barcode") or "").strip()
        for cargo in cargoes
        if str(cargo.get("gm_barcode") or "").strip()
    ]
    gm_barcodes = [str(cargo.get("gm_barcode") or "").strip() for cargo in kept_cargoes]
    gm_barcodes = [barcode for barcode in gm_barcodes if barcode]
    ozon_gm_comment = f"ШК ГМ Ozon: {', '.join(gm_barcodes)}" if gm_barcodes else ""
    composition_by_key: dict[tuple[str, str], dict] = {}
    for cargo in kept_cargoes:
        for item in cargo.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("offer_id") or "").strip().casefold(),
                str(item.get("barcode") or "").strip().casefold(),
            )
            row = composition_by_key.setdefault(
                key,
                {
                    "offer_id": str(item.get("offer_id") or "").strip(),
                    "barcode": str(item.get("barcode") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                    "quantity": 0,
                },
            )
            row["quantity"] += _positive_int(item.get("quantity"))
    composition = list(composition_by_key.values())

    payload = dict(result.get("payload") or {})
    payload_form = dict(payload.get("form") or {})
    payload_form["ozon_gm_comment"] = ozon_gm_comment
    payload["form"] = payload_form
    payload["gm_barcodes"] = gm_barcodes
    payload["gm_cargoes"] = kept_cargoes
    payload["ozon_boxes_total"] = len(kept_cargoes)
    payload["composition"] = composition
    result.update(
        {
            "payload": payload,
            "ozon_gm_comment": ozon_gm_comment,
            "gm_barcodes": gm_barcodes,
            "gm_cargoes": kept_cargoes,
            "boxes_total": len(kept_cargoes),
            "composition": composition,
        }
    )
    if gm_barcodes != original_gm_barcodes:
        result["ozon_gm_reconciled_after_item_removal"] = True
        result["removed_gm_barcodes"] = [
            barcode for barcode in original_gm_barcodes if barcode not in gm_barcodes
        ]
    kept_order_ids = list(
        dict.fromkeys(
            str(cargo.get("ozon_order_id") or "").strip()
            for cargo in kept_cargoes
            if str(cargo.get("ozon_order_id") or "").strip()
        )
    )
    if kept_order_ids:
        result["order_ids"] = kept_order_ids
        result["order_id"] = kept_order_ids[0]
    return result, []


def _ozon_supply_identity_values(order: ShippingOrder) -> list[str]:
    values = [
        getattr(order, "supply_number", ""),
        getattr(order, "shipping_barcode", ""),
        getattr(order, "wb_supply_barcode", ""),
    ]
    for line in str(getattr(order, "comment", "") or "").splitlines():
        text = line.strip()
        for label in ("Номер поставки Ozon:", "Номера поставок Ozon:"):
            if text.startswith(label):
                values.extend(part.strip() for part in re.split(r"[,;]", text[len(label):]) if part.strip())
    return list(
        dict.fromkeys(
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        )
    )


def _find_active_ozon_supply_duplicate_for_update(order: ShippingOrder) -> ShippingOrder | None:
    values = _ozon_supply_identity_values(order)
    if not values or not order.agency_id:
        return None
    lookup = Q(supply_number__in=values) | Q(shipping_barcode__in=values) | Q(wb_supply_barcode__in=values)
    for value in values:
        lookup |= Q(comment__icontains=value)
    qs = (
        ShippingOrder.objects.select_for_update()
        .filter(agency_id=order.agency_id)
        .exclude(status=ShippingOrder.STATUS_CANCELED)
        .filter(lookup)
    )
    if order.pk:
        qs = qs.exclude(pk=order.pk)
    matches = [
        match
        for match in qs.order_by("-id")[:50]
        if set(values).intersection(_ozon_supply_identity_values(match))
    ]
    for match in matches:
        if match.status == ShippingOrder.STATUS_DRAFT:
            return match
    return matches[0] if matches else None


def _copy_shipping_form_values(target: ShippingOrder, source: ShippingOrder) -> ShippingOrder:
    skip_fields = {
        "id",
        "number",
        "created_by",
        "status",
        "reserved_at",
        "shipped_at",
        "created_at",
        "updated_at",
    }
    for field in ShippingOrder._meta.concrete_fields:
        if field.name in skip_fields or field.primary_key:
            continue
        setattr(target, field.attname, getattr(source, field.attname))
    return target


def _clear_shipping_draft_after_delivery_type_change(order: ShippingOrder) -> None:
    """Clear client-entered draft data while preserving identity and attachments."""
    order.marketplace = None
    order.slot_date = None
    order.slot_time = None
    order.eta_at = None
    order.destination_address = ""
    order.planned_ship_date = None
    order.vehicle_type = ""
    order.vehicle_number = ""
    order.driver_phone = ""
    order.wb_supply_barcode = ""
    order.wb_transit_warehouse = False
    order.transit_address = ""
    order.shipping_barcode = ""
    order.destination_warehouse = ""
    order.supply_type = ""
    order.supply_number = ""
    order.distribution_status = ""
    order.expected_boxes = 0
    order.place_type = ""
    order.comment = ""


def _strip_ozon_api_comment_block(comment: str) -> str:
    text = str(comment or "").strip()
    marker = _OZON_API_COMMENT_TITLE
    if marker not in text:
        return text
    return text.split(marker, 1)[0].rstrip()


def _comment_with_ozon_api_meta(comment: str, meta: dict) -> str:
    if not meta or not (meta.get("supply_numbers") or meta.get("supply_number") or meta.get("shipping_barcode") or meta.get("gm_barcodes") or meta.get("boxes_total")):
        return str(comment or "").strip()
    base = _strip_ozon_api_comment_block(comment)
    lines = [_OZON_API_COMMENT_TITLE]
    supply_numbers = _as_clean_list(meta.get("supply_numbers"))
    if len(supply_numbers) > 1:
        lines.append(f"Поставок Ozon: {len(supply_numbers)}")
        lines.append(f"Номера поставок Ozon: {', '.join(supply_numbers)}")
    elif meta.get("supply_number"):
        lines.append(f"Номер поставки Ozon: {meta['supply_number']}")
    if meta.get("shipping_barcode"):
        lines.append(f"ШК поставки Ozon: {meta['shipping_barcode']}")
    if meta.get("boxes_total"):
        lines.append(f"Коробов Ozon: {int(meta['boxes_total'])}")
    if meta.get("gm_barcodes"):
        lines.append(f"ШК ГМ Ozon: {', '.join(meta['gm_barcodes'])}")
    if meta.get("destinations"):
        lines.append(f"Склады Ozon: {', '.join(meta['destinations'][:12])}")
    block = "\n".join(lines)
    return f"{base}\n\n{block}".strip() if base else block


def _ozon_meta_for_reserve(meta: dict | None) -> dict | None:
    """Keep combined Ozon GM structure in audit without inventing box bindings."""
    if not meta or not meta.get("is_combined"):
        return meta
    reserve_meta = dict(meta)
    reserve_meta["gm_cargoes"] = []
    return reserve_meta


def _value_from_ozon_api_comment(comment: str, label: str) -> str:
    prefix = f"{label}:"
    for line in str(comment or "").splitlines():
        text = line.strip()
        if text.startswith(prefix):
            return text.split(":", 1)[1].strip()
    return ""


def _extract_ozon_gm_codes_from_text(*values) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if not text:
            continue
        for line in text.splitlines() or [text]:
            match = re.search(r"ШК\s*ГМ(?:\s*Ozon)?\s*:\s*(.+)$", line, re.IGNORECASE)
            if not match:
                continue
            for code in _as_clean_list(match.group(1)):
                if code not in result:
                    result.append(code)
    return result


def _box_count_from_comment(comment: str | None) -> int:
    match = _BOX_COUNT_COMMENT_RE.search(str(comment or ""))
    if not match:
        return 0
    try:
        return int(match.group(1) or 0)
    except (TypeError, ValueError):
        return 0


def _ozon_item_signature_key(
    *,
    barcode: str = "",
    offer_id: str = "",
    barcode_aliases: dict[str, dict] | None = None,
) -> str:
    item_key, _display_barcode = _catalog_item_identity(
        barcode=barcode,
        sku_code=offer_id,
        barcode_aliases=barcode_aliases,
    )
    return item_key


def _ozon_reserved_fullbox_boxes(order: ShippingOrder) -> list[dict]:
    order_key = str(getattr(order, "number", "") or "").strip()
    agency_id = int(getattr(order, "agency_id", 0) or 0)
    if not order_key or not agency_id:
        return []

    reserved_rows = list(
        WarehouseStockSnapshot.objects.filter(
            agency_id=agency_id,
            is_archived=False,
            last_event__stock_context_type="shipping",
            last_event__stock_context_id=order_key,
            warehouse_state_code__in=[
                WarehouseStateCode.RESERVED_FOR_SHIPPING.value,
                WarehouseStateCode.MOVING_TO_OTG.value,
            ],
            shipping_reserved_qty__gt=0,
            container_id__isnull=False,
        ).values(
            "id",
            "container_id",
            "container__container_code",
            "sku_code",
            "name",
            "size",
            "barcode",
            "goods_type",
            "qty",
            "shipping_reserved_qty",
        )
    )
    if not reserved_rows:
        return []

    candidate_ids = {int(row["id"]) for row in reserved_rows}
    container_ids = {int(row["container_id"]) for row in reserved_rows}
    physical_rows = WarehouseStockSnapshot.objects.filter(
        agency_id=agency_id,
        container_id__in=container_ids,
        is_archived=False,
        qty__gt=0,
    ).values("id", "container_id", "qty", "shipping_reserved_qty")
    whole_box_container_ids: set[int] = set(container_ids)
    for row in physical_rows:
        container_id = int(row["container_id"])
        if (
            int(row["id"]) not in candidate_ids
            or int(row.get("shipping_reserved_qty") or 0) != int(row.get("qty") or 0)
        ):
            whole_box_container_ids.discard(container_id)

    boxes: dict[int, dict] = {}
    item_rows: dict[tuple, dict] = {}
    for row in reserved_rows:
        container_id = int(row["container_id"])
        if container_id not in whole_box_container_ids:
            continue
        box_code = str(row.get("container__container_code") or "").strip()
        if not box_code:
            continue
        box = boxes.setdefault(
            container_id,
            {"box_code": box_code, "qty": 0, "items": []},
        )
        item_key = (
            container_id,
            str(row.get("sku_code") or "").strip(),
            str(row.get("name") or "").strip(),
            str(row.get("size") or "").strip(),
            str(row.get("barcode") or "").strip(),
            str(row.get("goods_type") or "").strip(),
        )
        item = item_rows.get(item_key)
        if item is None:
            item = {
                "sku_code": item_key[1],
                "name": item_key[2],
                "size": item_key[3],
                "barcode": item_key[4],
                "goods_type": item_key[5],
                "qty": 0,
            }
            item_rows[item_key] = item
            box["items"].append(item)
        item_qty = int(row.get("shipping_reserved_qty") or 0)
        item["qty"] += item_qty
        box["qty"] += item_qty
    return sorted(boxes.values(), key=lambda box: str(box.get("box_code") or "").casefold())


def _ozon_gm_rows_with_exact_fullbox_match(
    order: ShippingOrder,
    gm_cargoes: list[dict],
    *,
    fullbox_boxes: list[dict] | None = None,
) -> list[dict]:
    barcode_values: list[str] = []
    sku_codes: list[str] = []
    try:
        barcode_values.extend(
            str(getattr(item, "barcode", "") or "").strip()
            for item in order.items.all()
        )
        sku_codes.extend(
            str(getattr(item, "sku_code", "") or "").strip()
            for item in order.items.all()
        )
    except Exception:
        pass
    for source_box in fullbox_boxes or []:
        if not isinstance(source_box, dict):
            continue
        barcode_values.extend(
            str(source_item.get("barcode") or "").strip()
            for source_item in (source_box.get("items") or [])
            if isinstance(source_item, dict)
        )
        sku_codes.extend(
            str(source_item.get("sku_code") or source_item.get("sku") or "").strip()
            for source_item in (source_box.get("items") or [])
            if isinstance(source_item, dict)
        )
    for source_cargo in gm_cargoes:
        if not isinstance(source_cargo, dict):
            continue
        barcode_values.extend(
            str(source_item.get("barcode") or "").strip()
            for source_item in (source_cargo.get("items") or [])
            if isinstance(source_item, dict)
        )
        sku_codes.extend(
            str(source_item.get("offer_id") or "").strip()
            for source_item in (source_cargo.get("items") or [])
            if isinstance(source_item, dict)
        )
    barcode_aliases = _catalog_barcode_alias_map(
        order,
        barcode_values,
        sku_codes,
    )

    box_composition: dict[str, defaultdict[str, int]] = {}
    if fullbox_boxes is not None:
        for source_box in fullbox_boxes:
            if not isinstance(source_box, dict):
                continue
            box_code = str(source_box.get("box_code") or source_box.get("code") or "").strip()
            if not box_code:
                continue
            composition = box_composition.setdefault(box_code, defaultdict(int))
            for source_item in source_box.get("items") or []:
                if not isinstance(source_item, dict):
                    continue
                item_key = _ozon_item_signature_key(
                    barcode=source_item.get("barcode"),
                    offer_id=source_item.get("sku_code") or source_item.get("offer_id"),
                    barcode_aliases=barcode_aliases,
                )
                quantity = int(source_item.get("qty") or source_item.get("quantity") or 0)
                if item_key and quantity > 0:
                    composition[item_key] += quantity
    else:
        try:
            items = list(order.items.all())
        except Exception:
            items = []
        for item in items:
            comment = str(getattr(item, "comment", "") or "")
            if extract_partial_box_split(comment) or "Исходные короба:" in comment:
                continue
            box_codes = _box_codes_from_item_comment(comment)
            requested = int(getattr(item, "qty_requested", 0) or 0)
            if not box_codes or requested <= 0 or requested % len(box_codes):
                continue
            item_key = _ozon_item_signature_key(
                barcode=getattr(item, "barcode", ""),
                offer_id=getattr(item, "sku_code", ""),
                barcode_aliases=barcode_aliases,
            )
            if not item_key:
                continue
            qty_per_box = requested // len(box_codes)
            for box_code in box_codes:
                box_composition.setdefault(box_code, defaultdict(int))[item_key] += qty_per_box

    boxes_by_signature: defaultdict[tuple, list[str]] = defaultdict(list)
    for box_code, composition in box_composition.items():
        signature = tuple(sorted((key, int(qty)) for key, qty in composition.items() if int(qty) > 0))
        if signature:
            boxes_by_signature[signature].append(box_code)

    rows: list[dict] = []
    row_indexes_by_signature: defaultdict[tuple, list[int]] = defaultdict(list)
    for source in gm_cargoes:
        row = dict(source)
        cargo_composition: defaultdict[str, int] = defaultdict(int)
        for cargo_item in row.get("items") or []:
            item_key = _ozon_item_signature_key(
                barcode=cargo_item.get("barcode"),
                offer_id=cargo_item.get("offer_id"),
                barcode_aliases=barcode_aliases,
            )
            quantity = int(cargo_item.get("quantity") or 0)
            if item_key and quantity > 0:
                cargo_composition[item_key] += quantity
        signature = tuple(
            sorted((key, int(qty)) for key, qty in cargo_composition.items() if int(qty) > 0)
        )
        row_index = len(rows)
        rows.append(row)
        if signature:
            row_indexes_by_signature[signature].append(row_index)

    assignments: dict[int, str] = {}
    for signature, row_indexes in row_indexes_by_signature.items():
        candidates = sorted(boxes_by_signature.get(signature, []), key=str.casefold)
        assignments.update(zip(row_indexes, candidates))

    for row_index, row in enumerate(rows):
        box_code = assignments.get(row_index, "")
        if box_code:
            row["fullbox_box_code"] = box_code
            row["assignment_status"] = "matched"
            row["assignment_label"] = "Точное совпадение состава"
        else:
            row["fullbox_box_code"] = ""
            row["assignment_status"] = "unassigned"
            row["assignment_label"] = "ШК ГМ пока не назначен коробу Fullbox"
    return rows


def _is_current_ozon_shipping(order: ShippingOrder | None) -> bool:
    if order is None:
        return False
    marketplace_name = str(
        getattr(getattr(order, "marketplace", None), "name", "") or ""
    ).strip().lower()
    return (
        str(getattr(order, "delivery_type", "") or "").strip()
        == ShippingOrder.DELIVERY_MARKETPLACE
        and marketplace_name == "ozon"
    )


def _request_posts_ozon_shipping(request) -> bool:
    """Return True when the submitted form represents an Ozon shipment."""
    if request.method != "POST":
        return False
    delivery_type = str(request.POST.get("delivery_type") or "").strip()
    if delivery_type != ShippingOrder.DELIVERY_MARKETPLACE:
        return False
    marketplace_value = str(request.POST.get("marketplace") or "").strip()
    if not marketplace_value:
        return False
    marketplace_name = marketplace_value
    if marketplace_value.isdigit():
        marketplace_name = (
            Market.objects.filter(pk=int(marketplace_value))
            .values_list("name", flat=True)
            .first()
            or ""
        )
    return str(marketplace_name).strip().casefold() in {"ozon", "озон"}


def _normalize_client_ozon_rework_eta_post(
    request,
    *,
    scope: str,
    edit_order: ShippingOrder | None,
    action_name: str,
    autosave: bool,
) -> bool:
    """Repair a stale hidden ship date before the client form is validated.

    The client UI renders a visible date input and submits ``eta_at`` through a
    hidden input.  A browser-restored or legacy Ozon rework draft can therefore
    post the old, now forbidden date even though the visible date was changed.
    For a final client edit only, use the valid Ozon slot date when the posted
    ship date is empty or older than the current marketplace deadline.

    This normalizes form input only.  It does not touch stock, reserves or the
    warehouse workflow.
    """
    if (
        request.method != "POST"
        or scope != "client"
        or autosave
        or action_name not in {"", "submit", "update"}
        or not _is_current_ozon_shipping(edit_order)
    ):
        return False

    def parse_date(value) -> date | None:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value or "").strip())
        if not match:
            return None
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None

    slot_date = parse_date(request.POST.get("slot_date"))
    posted_eta_date = parse_date(request.POST.get("eta_at"))
    if slot_date is None:
        return False

    from .forms import NEXT_DAY_DEADLINE_HOUR

    now_local = timezone.localtime()
    lead_days = 1 if now_local.hour < NEXT_DAY_DEADLINE_HOUR else 2
    minimum_ship_date = now_local.date() + timedelta(days=lead_days)
    if slot_date < minimum_ship_date:
        return False
    if posted_eta_date is not None and posted_eta_date >= minimum_ship_date:
        return False

    post = request.POST.copy()
    previous_value = str(post.get("eta_at") or "").strip()
    post["eta_at"] = f"{slot_date.isoformat()}T00:00"
    request.POST = post
    logger.info(
        "Normalized stale client Ozon ship date before validation "
        "order_pk=%s previous_eta=%s normalized_eta=%s",
        getattr(edit_order, "pk", None),
        previous_value[:32],
        post["eta_at"],
    )
    return True


def _combine_live_ozon_supply_payloads(payloads: list[dict]) -> dict:
    """Combine freshly loaded Ozon supplies using the client-form contract."""
    rows = [row for row in payloads or [] if isinstance(row, dict)]
    if not rows:
        return {}

    def unique(values) -> list[str]:
        return list(
            dict.fromkeys(
                str(value or "").strip()
                for value in values
                if str(value or "").strip()
            )
        )

    first = rows[0]
    first_form = first.get("form") if isinstance(first.get("form"), dict) else {}
    supply_numbers = unique(
        row.get("order_number")
        or (row.get("form") or {}).get("supply_number")
        or row.get("order_id")
        for row in rows
    )
    order_ids = unique(row.get("order_id") for row in rows)
    compact_label = (
        f"{supply_numbers[0]} + ещё {len(supply_numbers) - 1}"
        if len(supply_numbers) > 1
        else (supply_numbers[0] if supply_numbers else "Поставка Ozon")
    )
    composition: list[dict] = []
    gm_cargoes: list[dict] = []
    selected_boxes: Counter[str] = Counter()
    partial_box_splits: list[dict] = []
    destinations: list[dict] = []
    destination_warehouses: list[str] = []
    gm_barcodes: list[str] = []
    discrepancies: list[str] = []
    supplies: list[dict] = []
    piece_pick_total_qty = 0
    ozon_boxes_total = 0

    for row in rows:
        form_data = row.get("form") if isinstance(row.get("form"), dict) else {}
        supply_number = str(
            row.get("order_number")
            or form_data.get("supply_number")
            or row.get("order_id")
            or ""
        ).strip()
        order_id = str(row.get("order_id") or "").strip()
        source_meta = {
            "ozon_supply_number": supply_number,
            "ozon_order_id": order_id,
        }
        for item in row.get("composition") or row.get("items") or []:
            if isinstance(item, dict):
                composition.append({**item, **source_meta})
        for cargo in row.get("gm_cargoes") or []:
            if isinstance(cargo, dict):
                gm_cargoes.append({**cargo, **source_meta})
        for key, value in (row.get("selected_boxes") or {}).items():
            try:
                selected_boxes[str(key)] += max(int(value or 0), 0)
            except (TypeError, ValueError):
                continue
        for split in row.get("partial_box_splits") or []:
            if isinstance(split, dict):
                partial_box_splits.append({**split, **source_meta})
        for destination in row.get("destinations") or []:
            if isinstance(destination, dict):
                destinations.append({**destination, **source_meta})
        destination_warehouses.extend(
            str(value or "").strip()
            for value in row.get("destination_warehouses") or []
            if str(value or "").strip()
        )
        gm_barcodes.extend(
            str(value or "").strip()
            for value in row.get("gm_barcodes") or []
            if str(value or "").strip()
        )
        discrepancies.extend(
            str(value or "").strip()
            for value in row.get("discrepancies") or []
            if str(value or "").strip()
        )
        try:
            piece_pick_total_qty += max(int(row.get("piece_pick_total_qty") or 0), 0)
            ozon_boxes_total += max(int(row.get("ozon_boxes_total") or 0), 0)
        except (TypeError, ValueError):
            pass
        supplies.append(
            {
                "order_id": order_id,
                "order_number": supply_number,
                "form": {
                    "supply_number": str(form_data.get("supply_number") or supply_number).strip(),
                    "shipping_barcode": str(form_data.get("shipping_barcode") or "").strip(),
                    "slot_date": str(form_data.get("slot_date") or "").strip(),
                    "slot_time": str(form_data.get("slot_time") or "").strip(),
                    "destination_warehouse": str(form_data.get("destination_warehouse") or "").strip(),
                    "destination_address": str(form_data.get("destination_address") or "").strip(),
                    "transit_address": str(form_data.get("transit_address") or "").strip(),
                },
                "destination_warehouses": unique(row.get("destination_warehouses") or []),
                "gm_barcodes": unique(row.get("gm_barcodes") or []),
                "ozon_boxes_total": max(int(row.get("ozon_boxes_total") or 0), 0),
            }
        )

    warehouses = unique(destination_warehouses)
    gm_barcodes = unique(gm_barcodes)
    slot_values = unique(
        " ".join(
            value
            for value in (
                str((row.get("form") or {}).get("slot_date") or "").strip(),
                str((row.get("form") or {}).get("slot_time") or "").strip(),
            )
            if value
        )
        for row in rows
    )
    slot_date = str(first_form.get("slot_date") or "").strip()
    slot_time = str(first_form.get("slot_time") or "").strip()
    destination_label = (
        warehouses[0]
        if len(warehouses) == 1
        else ("Несколько складов Ozon" if warehouses else str(first_form.get("destination_warehouse") or "").strip())
    )
    matched = sum(1 for row in composition if row.get("matched"))
    combined_form = {
        **first_form,
        "shipping_barcode": compact_label,
        "supply_number": compact_label,
        "slot_date": slot_date,
        "slot_time": slot_time,
        "destination_warehouse": destination_label,
        "destination_address": (
            "Несколько складов Ozon"
            if len(warehouses) > 1
            else str(first_form.get("destination_address") or "").strip()
        ),
        "transit_address": (
            "Несколько складов Ozon"
            if len(warehouses) > 1
            else str(first_form.get("transit_address") or "").strip()
        ),
        "eta_date": slot_date,
        "ozon_gm_comment": (
            f"ШК ГМ Ozon: {', '.join(gm_barcodes)}" if gm_barcodes else ""
        ),
    }
    return {
        "ok": True,
        "order_id": order_ids[0] if order_ids else "",
        "order_ids": order_ids,
        "order_number": compact_label,
        "supply_numbers": supply_numbers,
        "supplies": supplies,
        "is_combined_ozon_supply": len(rows) > 1,
        "state": "combined" if len(rows) > 1 else str(first.get("state") or ""),
        "form": combined_form,
        "destinations": destinations,
        "destination_warehouses": warehouses,
        "is_multi_destination": len(warehouses) > 1,
        "items": composition,
        "composition": composition,
        "gm_cargoes": gm_cargoes,
        "gm_barcodes": gm_barcodes,
        "ozon_boxes_total": ozon_boxes_total or len(gm_barcodes),
        "selected_boxes": {
            key: str(value) for key, value in selected_boxes.items() if value > 0
        },
        "partial_box_splits": partial_box_splits,
        "piece_pick_total_qty": piece_pick_total_qty,
        "piece_pick_positions": len(partial_box_splits),
        "requires_piece_pick_confirmation": bool(partial_box_splits),
        "discrepancies": unique(discrepancies),
        "stock_match": {
            "status": "full" if composition and matched == len(composition) else "partial",
            "total": len(composition),
            "matched": matched,
            "missing": max(len(composition) - matched, 0),
        },
        "slot_values": slot_values,
    }


def _refresh_client_ozon_post_from_api(
    request,
    *,
    scope: str,
    selected_client,
    edit_order: ShippingOrder | None,
    action_name: str,
    autosave: bool,
    stock_rows: list[dict],
) -> tuple[bool, str]:
    """Replace final client-LK POST values with a fresh read-only Ozon snapshot.

    Managers can open the same client form through ``?client=<agency>``.  That
    route must use the identical final Ozon refresh; otherwise it validates
    stale browser box selections against the current GM composition.
    """
    if (
        request.method != "POST"
        or selected_client is None
        or not _shipping_use_client_lk_form(
            scope=scope,
            request=request,
            selected_client=selected_client,
        )
        or autosave
        or action_name not in {"", "submit", "update"}
        or not (_request_posts_ozon_shipping(request) or _is_current_ozon_shipping(edit_order))
    ):
        return False, ""

    raw_payload = str(request.POST.get("ozon_supply_payload_json") or "").strip()
    payload: dict = {}
    if raw_payload:
        try:
            parsed = json.loads(raw_payload)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    # The separate Ozon Excel multi-warehouse flow keeps its source in the
    # session and intentionally has no API payload in this form field.
    if not payload and edit_order is None:
        return False, ""

    order_id_values: list = []
    order_id_values.extend(payload.get("order_ids") or [])
    order_id_values.append(payload.get("order_id"))
    for supply in payload.get("supplies") or []:
        if isinstance(supply, dict):
            order_id_values.append(supply.get("order_id"))
    if edit_order is not None:
        saved_meta = _saved_ozon_api_intake_meta(edit_order)
        order_id_values.extend(saved_meta.get("order_ids") or [])
        order_id_values.append(saved_meta.get("order_id"))
    order_ids: list[int] = []
    for value in order_id_values:
        text = str(value or "").strip()
        if text.isdigit() and int(text) > 0 and int(text) not in order_ids:
            order_ids.append(int(text))
    if not order_ids:
        fallback_values = [
            payload.get("order_number"),
            (payload.get("form") or {}).get("supply_number"),
            request.POST.get("supply_number"),
            *(_ozon_supply_identity_values(edit_order) if edit_order is not None else []),
        ]
        for value in fallback_values:
            text = re.sub(r"\s+", "", str(value or ""))
            if text.isdigit() and int(text) > 0:
                order_ids.append(int(text))
                break
    if not order_ids:
        return False, (
            "Не удалось определить исходную заявку Ozon. "
            "Выберите поставку Ozon заново; заявка Fullbox не сохранена."
        )
    if len(order_ids) > 10:
        return False, "В одной заявке можно обновить не более 10 поставок Ozon."

    from .ozon_supplies import (
        get_ozon_supply_for_agency,
        rebalance_ozon_supply_payloads,
    )

    live_rows: list[dict] = []
    for order_id in order_ids:
        try:
            result = get_ozon_supply_for_agency(
                selected_client,
                order_id,
                stock_rows=stock_rows,
                enrich_gm=False,
                api_timeout=10,
                bundle_max_pages=4,
                include_gm_cargoes=True,
                gm_enrich_limit=8,
                gm_pause_seconds=1.05,
                exclude_order_id=(getattr(edit_order, "pk", None) if edit_order is not None else None),
                # Each source is loaded independently.  Physical Fullbox boxes
                # are allocated once against the summed batch below.
                already_selected={},
            )
        except Exception:
            logger.exception(
                "Could not refresh client Ozon source before submit agency=%s order_id=%s",
                getattr(selected_client, "id", None),
                order_id,
            )
            return False, (
                "Не удалось обновить данные поставки из Ozon. "
                "Заявка Fullbox не сохранена; повторите отправку позже."
            )
        if not result.get("ok"):
            logger.warning(
                "Client Ozon source refresh rejected agency=%s order_id=%s error=%s",
                getattr(selected_client, "id", None),
                order_id,
                str(result.get("error") or "")[:300],
            )
            return False, (
                "Ozon не подтвердил актуальные данные поставки. "
                "Заявка Fullbox не сохранена; обновите выбор поставки и повторите отправку."
            )
        if result.get("already_uploaded"):
            existing = result.get("existing_shipping") or {}
            label = str(existing.get("number") or existing.get("id") or "").strip()
            return False, (
                "Эта поставка Ozon уже используется в другой заявке Fullbox"
                + (f" {label}" if label else "")
                + ". Новая заявка не сохранена."
            )
        form_data = result.get("form") if isinstance(result.get("form"), dict) else {}
        cargoes = _normalize_ozon_gm_cargoes(result.get("gm_cargoes"))
        try:
            boxes_total = max(int(result.get("ozon_boxes_total") or 0), 0)
        except (TypeError, ValueError):
            boxes_total = 0
        snapshot_complete = bool(
            form_data.get("supply_number")
            and form_data.get("slot_date")
            and cargoes
            and boxes_total == len(cargoes)
            and all(cargo.get("items") for cargo in cargoes)
        )
        if not snapshot_complete:
            ready_cargoes = sum(1 for cargo in cargoes if cargo.get("items"))
            if (
                form_data.get("supply_number")
                and form_data.get("slot_date")
                and cargoes
                and boxes_total == len(cargoes)
                and ready_cargoes < len(cargoes)
            ):
                return False, (
                    f"Состав ШК ГМ Ozon загружен: {ready_cargoes} из {len(cargoes)}. "
                    "Откройте выбор заявок Ozon и дождитесь завершения проверки; "
                    "заявка Fullbox пока не сохранена."
                )
            missing = []
            if not form_data.get("supply_number"):
                missing.append("номер поставки")
            if not form_data.get("slot_date"):
                missing.append("дата приёмки Ozon")
            if not cargoes:
                missing.append("короба (грузоместа)")
            elif boxes_total != len(cargoes):
                missing.append(f"все грузоместа: получено {len(cargoes)} из {boxes_total}")
            return False, (
                "В данных Ozon отсутствуют: " + ", ".join(missing or ["полный состав коробов"])
                + ". Проверьте эти данные в Ozon, затем нажмите «Выбрать заявки Ozon» и повторите проверку. "
                "Заявка Fullbox пока не сохранена."
            )
        live_rows.append(result)

    live_rows = rebalance_ozon_supply_payloads(
        live_rows,
        stock_rows,
        agency=selected_client,
    )
    if any(
        str((result.get("stock_match") or {}).get("status") or "") != "full"
        for result in live_rows
    ):
        return False, (
            "Состав поставки Ozon обновлён, но его нельзя полностью собрать "
            "из доступных остатков Fullbox. Заявка не сохранена."
        )
    if not any(
        result.get("selected_boxes") or result.get("partial_box_splits")
        for result in live_rows
    ):
        return False, (
            "Ozon передал состав поставки, но система не смогла подобрать товар "
            "из остатков Fullbox. Заявка не сохранена."
        )

    live_payload = (
        live_rows[0]
        if len(live_rows) == 1
        else _combine_live_ozon_supply_payloads(live_rows)
    )
    live_form = live_payload.get("form") if isinstance(live_payload.get("form"), dict) else {}
    expected_supply_values = {
        re.sub(r"\s+", "", value).casefold()
        for source in (
            payload.get("supply_numbers"),
            payload.get("order_number"),
            (payload.get("form") or {}).get("supply_number"),
            _ozon_supply_identity_values(edit_order) if edit_order is not None else [],
        )
        for value in _as_clean_list(source)
        if value
    }
    live_supply_values = {
        re.sub(r"\s+", "", value).casefold()
        for source in (
            live_payload.get("supply_numbers"),
            live_payload.get("order_number"),
            live_form.get("supply_number"),
        )
        for value in _as_clean_list(source)
        if value
    }
    if (
        expected_supply_values
        and live_supply_values
        and expected_supply_values.isdisjoint(live_supply_values)
    ):
        return False, (
            "Ozon вернул другую поставку вместо выбранной. "
            "Заявка Fullbox не сохранена; выберите поставку заново."
        )

    gm_barcodes = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in live_payload.get("gm_barcodes") or []
            if str(value or "").strip()
        )
    )
    gm_comment = str(
        live_form.get("ozon_gm_comment")
        or (f"ШК ГМ Ozon: {', '.join(gm_barcodes)}" if gm_barcodes else "")
    ).strip()
    post = request.POST.copy()
    post["ozon_supply_payload_json"] = json.dumps(
        live_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for field_name in (
        "shipping_barcode",
        "supply_number",
        "slot_date",
        "slot_time",
        "transit_address",
        "destination_warehouse",
        "vehicle_number",
        "driver_phone",
    ):
        post[field_name] = str(live_form.get(field_name) or "").strip()
    eta_date = str(live_form.get("eta_date") or live_form.get("slot_date") or "").strip()
    if eta_date:
        post["eta_at"] = f"{eta_date}T00:00"
    if live_form.get("wb_transit_warehouse"):
        post["wb_transit_warehouse"] = "1"
    else:
        post.pop("wb_transit_warehouse", None)
    post["ozon_gm_comment"] = gm_comment
    post["ozon_gm_barcodes"] = ", ".join(gm_barcodes)
    post["ozon_boxes_total"] = str(live_payload.get("ozon_boxes_total") or len(gm_barcodes))
    request.POST = post
    _inject_stock_box_selections(
        request,
        stock_rows,
        live_payload.get("selected_boxes") or {},
        partial_box_splits=live_payload.get("partial_box_splits") or [],
    )
    logger.info(
        "Refreshed client Ozon source before submit agency=%s order_pk=%s order_ids=%s slot=%s %s items=%s gm=%s",
        getattr(selected_client, "id", None),
        getattr(edit_order, "pk", None),
        order_ids,
        str(live_form.get("slot_date") or ""),
        str(live_form.get("slot_time") or ""),
        len(live_payload.get("composition") or live_payload.get("items") or []),
        len(gm_barcodes),
    )
    return True, ""


def _request_posts_marketplace_without_selection(request) -> bool:
    """Return True while a client has not selected a marketplace yet."""
    if request.method != "POST":
        return False
    delivery_type = str(request.POST.get("delivery_type") or "").strip()
    marketplace_value = str(request.POST.get("marketplace") or "").strip()
    return (
        delivery_type == ShippingOrder.DELIVERY_MARKETPLACE
        and not marketplace_value
    )


def _ozon_api_summary_for_form(
    order: ShippingOrder | None,
    *,
    fullbox_boxes: list[dict] | None = None,
) -> dict:
    if not _is_current_ozon_shipping(order):
        return {"show": False}
    comment = str(getattr(order, "comment", "") or "")
    marketplace_name = str(getattr(getattr(order, "marketplace", None), "name", "") or "").strip().lower()
    audit_supply_number = ""
    audit_shipping_barcode = ""
    audit_boxes_total = ""
    audit_gm_barcodes: list[str] = []
    audit_gm_cargoes: list[dict] = []
    audit_gm_comment = ""
    audit_destination_warehouses: list[str] = []
    audit_transit_address = ""
    try:
        from audit.models import OrderAuditEntry

        audit_entries = OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id=str(getattr(order, "number", "") or ""),
        ).order_by("-id")[:30]
        for entry in audit_entries:
            payload = entry.payload or {}
            if not isinstance(payload, dict):
                continue
            if not audit_supply_number:
                audit_supply_number = str(
                    payload.get("supply_number")
                    or payload.get("ozon_order_number")
                    or ""
                ).strip()
            if not audit_shipping_barcode:
                audit_shipping_barcode = str(
                    payload.get("shipping_barcode")
                    or payload.get("wb_supply_barcode")
                    or ""
                ).strip()
            if not audit_boxes_total:
                audit_boxes_total = str(payload.get("ozon_boxes_total") or "").strip()
            if not audit_gm_comment:
                audit_gm_comment = str(payload.get("ozon_gm_comment") or "").strip()
            if not audit_gm_barcodes:
                audit_gm_barcodes = _as_clean_list(
                    payload.get("gm_barcodes")
                    or payload.get("ozon_gm_barcodes")
                    or audit_gm_comment
                )
            if not audit_gm_cargoes:
                audit_gm_cargoes = _normalize_ozon_gm_cargoes(payload.get("gm_cargoes"))
            if not audit_destination_warehouses:
                payload_destinations = payload.get("destination_warehouses")
                if isinstance(payload_destinations, list):
                    audit_destination_warehouses = _as_clean_list(payload_destinations)
                else:
                    audit_destination = str(payload.get("destination_warehouse") or "").strip()
                    if audit_destination:
                        audit_destination_warehouses = [audit_destination]
            if not audit_transit_address:
                audit_transit_address = str(payload.get("transit_address") or "").strip()
    except Exception:
        pass
    # The newest audit row can describe a later client edit and therefore can
    # contain GM composition rewritten to match that edit.  Warehouse packing
    # must use the same trusted immutable intake snapshot as reservation and
    # acceptance, otherwise a valid delivered box can be matched to the wrong
    # GM cargo.
    trusted_intake = _saved_ozon_api_intake_meta(order)
    trusted_cargoes = _normalize_ozon_gm_cargoes(trusted_intake.get("gm_cargoes"))
    if trusted_cargoes:
        audit_gm_cargoes = trusted_cargoes
        audit_gm_barcodes = [
            str(row.get("gm_barcode") or "").strip()
            for row in trusted_cargoes
            if str(row.get("gm_barcode") or "").strip()
        ]
        audit_boxes_total = str(
            trusted_intake.get("boxes_total") or len(trusted_cargoes)
        )
        audit_supply_number = str(
            trusted_intake.get("order_number") or audit_supply_number
        ).strip()
        trusted_destinations = _as_clean_list(trusted_intake.get("destinations"))
        if trusted_destinations:
            audit_destination_warehouses = trusted_destinations
    is_ozon = (
        marketplace_name == "ozon"
        or "ozon" in marketplace_name
        or _OZON_API_COMMENT_TITLE in comment
        or bool(_value_from_ozon_api_comment(comment, "ШК ГМ Ozon"))
        or bool(audit_supply_number or audit_gm_barcodes or audit_gm_comment or audit_boxes_total)
    )
    if not is_ozon:
        return {"show": False}

    supply_number = (
        str(getattr(order, "supply_number", "") or "").strip()
        or _value_from_ozon_api_comment(comment, "Номер поставки Ozon")
        or audit_supply_number
        or str(getattr(order, "wb_supply_barcode", "") or "").strip()
    )
    shipping_barcode = (
        str(getattr(order, "shipping_barcode", "") or "").strip()
        or _value_from_ozon_api_comment(comment, "ШК поставки Ozon")
        or audit_shipping_barcode
    )
    gm_barcodes = _extract_ozon_gm_codes_from_text(
        _value_from_ozon_api_comment(comment, "ШК ГМ Ozon"),
        comment,
        audit_gm_comment,
    )
    for code in audit_gm_barcodes:
        if code not in gm_barcodes:
            gm_barcodes.append(code)
    item_rows: list[dict] = []
    boxes_from_items = 0
    try:
        items = list(order.items.all())
    except Exception:
        items = []
    for item in items:
        item_comment = str(getattr(item, "comment", "") or "")
        item_gm = _extract_ozon_gm_codes_from_text(item_comment)
        for code in item_gm:
            if code not in gm_barcodes:
                gm_barcodes.append(code)
        item_boxes = _box_count_from_comment(item_comment)
        boxes_from_items += int(max(item_boxes, 0))
        item_rows.append(
            {
                "sku_code": getattr(item, "sku_code", "") or "—",
                "name": getattr(item, "name", "") or "—",
                "barcode": getattr(item, "barcode", "") or "—",
                "qty_requested": int(getattr(item, "qty_requested", 0) or 0),
                "boxes": item_boxes,
                "gm_barcodes": ", ".join(item_gm) if item_gm else "",
            }
        )
    gm_cargoes = _ozon_gm_rows_with_exact_fullbox_match(
        order,
        audit_gm_cargoes,
        fullbox_boxes=fullbox_boxes,
    )
    gm_matched_count = sum(
        1 for row in gm_cargoes if str(row.get("assignment_status") or "") == "matched"
    )
    gm_unassigned_count = sum(
        1 for row in gm_cargoes if str(row.get("assignment_status") or "") == "unassigned"
    )
    boxes_total = (
        _value_from_ozon_api_comment(comment, "Коробов Ozon")
        or audit_boxes_total
        or str(int(getattr(order, "expected_boxes", 0) or 0) or boxes_from_items or "")
    )
    destination_warehouse = str(getattr(order, "destination_warehouse", "") or "").strip()
    destination_warehouses = []
    if destination_warehouse:
        destination_warehouses.append(destination_warehouse)
    for warehouse in _as_clean_list(_value_from_ozon_api_comment(comment, "Склады Ozon")):
        if warehouse and warehouse not in destination_warehouses:
            destination_warehouses.append(warehouse)
    for warehouse in audit_destination_warehouses:
        if warehouse and warehouse not in destination_warehouses:
            destination_warehouses.append(warehouse)
    destination_missing = not destination_warehouses
    transit_address = str(getattr(order, "transit_address", "") or "").strip() or audit_transit_address
    gm_comment = f"ШК ГМ Ozon: {', '.join(gm_barcodes)}" if gm_barcodes else ""
    payload = {
        "form": {
            "supply_number": supply_number,
            "shipping_barcode": shipping_barcode,
            "ozon_gm_comment": gm_comment,
            "slot_date": order.slot_date.isoformat() if order.slot_date else "",
            "slot_time": order.slot_time.strftime("%H:%M") if order.slot_time else "",
            "eta_date": (
                order.planned_ship_date.isoformat()
                if order.planned_ship_date
                else (
                    timezone.localtime(order.eta_at).date().isoformat()
                    if order.eta_at
                    else ""
                )
            ),
            "destination_warehouse": destination_warehouses[0] if destination_warehouses else "",
            "transit_address": transit_address,
        },
        "gm_barcodes": gm_barcodes,
        "gm_cargoes": gm_cargoes,
        "ozon_boxes_total": boxes_total,
        "destination_warehouses": destination_warehouses,
    }
    return {
        "show": bool(supply_number or shipping_barcode or gm_barcodes or gm_cargoes or boxes_total),
        "supply_number": supply_number,
        "shipping_barcode": shipping_barcode,
        "boxes_total": boxes_total,
        "destination_warehouse": destination_warehouses[0] if destination_warehouses else "",
        "destination_warehouses": destination_warehouses,
        "destination_missing": destination_missing,
        "transit_address": transit_address,
        "gm_barcodes": gm_barcodes,
        "gm_cargoes": gm_cargoes,
        "gm_matched_count": gm_matched_count,
        "gm_unassigned_count": gm_unassigned_count,
        "gm_barcodes_label": ", ".join(gm_barcodes),
        "gm_comment": gm_comment,
        "item_rows": item_rows,
        "payload_json": json.dumps(payload, ensure_ascii=False),
    }


def _ozon_loose_packing_gm_plan(
    order: ShippingOrder,
    *,
    loose_items: list[dict],
    repack_all_delivered: bool = False,
) -> dict:
    default = {
        "required": False,
        "ready": True,
        "targets": [],
        "errors": [],
        "target_count": 0,
    }
    if not _is_current_ozon_shipping(order):
        return default

    from .packing import _shipping_delivered_boxes

    errors: list[str] = []
    delivered_boxes = _shipping_delivered_boxes(order)
    ozon_summary = _ozon_api_summary_for_form(order, fullbox_boxes=delivered_boxes)
    gm_cargoes = [
        dict(row)
        for row in ozon_summary.get("gm_cargoes") or []
        if isinstance(row, dict)
    ]
    if not gm_cargoes:
        errors.append("Ozon не передал состав ШК ГМ. Формирование коробов остановлено без изменений.")

    barcode_values = [
        item.get("barcode")
        for item in loose_items
        if isinstance(item, dict)
    ]
    sku_codes = [
        item.get("sku_code")
        for item in loose_items
        if isinstance(item, dict)
    ]
    for cargo in gm_cargoes:
        for item in cargo.get("items") or []:
            if not isinstance(item, dict):
                continue
            barcode_values.append(item.get("barcode"))
            sku_codes.append(item.get("offer_id"))
    barcode_aliases = _catalog_barcode_alias_map(
        order,
        barcode_values,
        sku_codes,
    )

    # Ozon's offer_id is a marketplace article and may legitimately differ
    # from the client's catalog article even when the exact product barcode is
    # the same.  Relax that article check only when the barcode maps to one
    # active client SKU and the foreign offer_id is not itself another SKU in
    # the same catalog.  Ambiguous or conflicting catalog data stays blocked.
    from django.db.models.functions import Lower
    from sku.models import SKU, SKUBarcode

    agency_id = int(getattr(order, "agency_id", 0) or 0)
    barcode_sku_ids: defaultdict[str, set[int]] = defaultdict(set)
    clean_barcode_values = sorted(
        {
            str(value or "").strip()
            for value in barcode_values
            if str(value or "").strip()
        }
    )
    if agency_id and clean_barcode_values:
        for value, sku_id in SKUBarcode.objects.filter(
            sku__agency_id=agency_id,
            sku__deleted=False,
            value__in=clean_barcode_values,
        ).values_list("value", "sku_id"):
            barcode_sku_ids[str(value or "").strip().casefold()].add(int(sku_id))
    unambiguous_catalog_barcodes = {
        barcode_key
        for barcode_key, catalog_sku_ids in barcode_sku_ids.items()
        if len(catalog_sku_ids) == 1
    }
    clean_sku_code_keys = {
        str(value or "").strip().casefold()
        for value in sku_codes
        if str(value or "").strip()
    }
    known_catalog_sku_code_keys = set()
    if agency_id and clean_sku_code_keys:
        known_catalog_sku_code_keys = set(
            SKU.objects.filter(agency_id=agency_id, deleted=False)
            .annotate(normalized_sku_code=Lower("sku_code"))
            .filter(normalized_sku_code__in=clean_sku_code_keys)
            .values_list("normalized_sku_code", flat=True)
        )

    def item_identity(
        item: dict,
        *,
        sku_field: str,
        marketplace_offer: bool = False,
    ) -> str:
        barcode = str(item.get("barcode") or "").strip()
        sku_code = str(item.get(sku_field) or "").strip()
        if marketplace_offer:
            barcode_key = barcode.casefold()
            sku_code_key = sku_code.casefold()
            alias = barcode_aliases.get(barcode_key) or {}
            catalog_sku_code_key = str(alias.get("sku_code_key") or "").strip().casefold()
            if (
                alias
                and barcode_key in unambiguous_catalog_barcodes
                and sku_code_key
                and catalog_sku_code_key
                and sku_code_key != catalog_sku_code_key
                and sku_code_key not in known_catalog_sku_code_keys
            ):
                sku_code = ""
        identity, _display_barcode = _catalog_item_identity(
            barcode=barcode,
            sku_code=sku_code,
            barcode_aliases=barcode_aliases,
        )
        return identity

    # One SKU may legitimately be present under several catalog aliases (for
    # example EAN-13, GTIN-14 and an OZN barcode).  Keep the physical source
    # rows separate so packing and KIZ checks retain their exact snapshot key,
    # but compare Ozon cargo composition by the shared catalog identity.
    loose_by_identity: defaultdict[str, list[dict]] = defaultdict(list)
    loose_totals: Counter[str] = Counter()
    for loose_item in loose_items:
        if not isinstance(loose_item, dict):
            continue
        barcode = str(loose_item.get("barcode") or "").strip()
        quantity = int(loose_item.get("qty") or 0)
        if not barcode or quantity <= 0:
            errors.append("У товара без короба отсутствует однозначный ШК или количество.")
            continue
        identity = item_identity(loose_item, sku_field="sku_code")
        if not identity:
            errors.append(f"Не удалось определить товар по ШК {barcode}.")
            continue
        loose_by_identity[identity].append(
            {
                "item": dict(loose_item),
                "remaining": quantity,
            }
        )
        loose_totals[identity] += quantity

    def signature(
        items: list[dict],
        *,
        quantity_key: str,
        sku_field: str,
        marketplace_offer: bool = False,
    ) -> tuple:
        totals: Counter[str] = Counter()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            identity = item_identity(
                item,
                sku_field=sku_field,
                marketplace_offer=marketplace_offer,
            )
            quantity = int(item.get(quantity_key) or 0)
            if not identity or quantity <= 0:
                return ()
            totals[identity] += quantity
        return tuple(sorted(totals.items()))

    delivered_by_signature: defaultdict[tuple, list[dict]] = defaultdict(list)
    for box in ([] if repack_all_delivered else delivered_boxes):
        box_signature = signature(
            list(box.get("items") or []),
            quantity_key="qty",
            sku_field="sku_code",
        )
        if not box_signature:
            errors.append(
                f"У короба {str(box.get('box_code') or '-').strip() or '-'} нет точного состава по ШК."
            )
            continue
        delivered_by_signature[box_signature].append(box)

    cargoes_by_signature: defaultdict[tuple, list[dict]] = defaultdict(list)
    seen_gm: set[str] = set()
    for cargo in sorted(
        gm_cargoes,
        key=lambda row: (
            str(row.get("supply_id") or "").casefold(),
            str(row.get("gm_barcode") or "").casefold(),
        ),
    ):
        gm_barcode = str(cargo.get("gm_barcode") or "").strip()
        if not gm_barcode:
            errors.append("В данных Ozon найден груз без ШК ГМ.")
            continue
        gm_key = gm_barcode.casefold()
        if gm_key in seen_gm:
            errors.append(f"ШК ГМ {gm_barcode} указан Ozon несколько раз.")
            continue
        seen_gm.add(gm_key)
        cargo_signature = signature(
            list(cargo.get("items") or []),
            quantity_key="quantity",
            sku_field="offer_id",
            marketplace_offer=True,
        )
        if not cargo_signature:
            errors.append(f"Для ШК ГМ {gm_barcode} Ozon не передал точный состав.")
            continue
        cargoes_by_signature[cargo_signature].append(cargo)

    consumed_gm: set[str] = set()
    consumed_delivered_boxes: set[str] = set()
    for box_signature, boxes in delivered_by_signature.items():
        candidates = cargoes_by_signature.get(box_signature) or []
        match_count = min(len(boxes), len(candidates))
        consumed_gm.update(
            str(cargo.get("gm_barcode") or "").strip().casefold()
            for cargo in candidates[:match_count]
        )
        consumed_delivered_boxes.update(
            str(box.get("box_code") or box.get("code") or "").strip().casefold()
            for box in boxes[:match_count]
        )

    unmatched_cargoes = [
        cargo
        for cargoes in cargoes_by_signature.values()
        for cargo in cargoes
        if str(cargo.get("gm_barcode") or "").strip().casefold() not in consumed_gm
    ]
    unmatched_delivered = [
        box
        for boxes in delivered_by_signature.values()
        for box in boxes
        if str(box.get("box_code") or box.get("code") or "").strip().casefold()
        not in consumed_delivered_boxes
    ]

    def supplement_delta(cargo: dict, box: dict) -> tuple[tuple[str, int], ...]:
        cargo_signature = signature(
            list(cargo.get("items") or []),
            quantity_key="quantity",
            sku_field="offer_id",
            marketplace_offer=True,
        )
        box_signature = signature(
            list(box.get("items") or []),
            quantity_key="qty",
            sku_field="sku_code",
        )
        if not cargo_signature or not box_signature:
            return ()
        cargo_totals = dict(cargo_signature)
        box_totals = dict(box_signature)
        if any(
            barcode not in cargo_totals or quantity > int(cargo_totals.get(barcode) or 0)
            for barcode, quantity in box_totals.items()
        ):
            return ()
        delta = tuple(
            sorted(
                (barcode, int(quantity) - int(box_totals.get(barcode) or 0))
                for barcode, quantity in cargo_totals.items()
                if int(quantity) - int(box_totals.get(barcode) or 0) > 0
            )
        )
        if not delta:
            return ()
        if any(int(loose_totals.get(barcode) or 0) < quantity for barcode, quantity in delta):
            return ()
        return delta

    # A delivered Fullbox box may be an exact, strict subset of one Ozon cargo
    # (for example 24 units in the box plus 1 loose unit).  Allow the operator
    # to supplement that physical box only when both sides of the match are
    # unambiguous; otherwise keep the safety block instead of guessing.
    supplement_assignments: dict[str, dict] = {}
    remaining_cargoes = list(unmatched_cargoes)
    remaining_boxes = list(unmatched_delivered)
    while remaining_cargoes and remaining_boxes:
        cargo_candidates: dict[str, list[tuple[dict, tuple[tuple[str, int], ...]]]] = {}
        box_candidates: defaultdict[str, list[str]] = defaultdict(list)
        for cargo in remaining_cargoes:
            gm_barcode = str(cargo.get("gm_barcode") or "").strip()
            matches: list[tuple[dict, tuple[tuple[str, int], ...]]] = []
            for box in remaining_boxes:
                delta = supplement_delta(cargo, box)
                if not delta:
                    continue
                matches.append((box, delta))
                box_code = str(box.get("box_code") or box.get("code") or "").strip().casefold()
                box_candidates[box_code].append(gm_barcode.casefold())
            cargo_candidates[gm_barcode.casefold()] = matches

        resolved_pair = None
        for cargo in remaining_cargoes:
            gm_barcode = str(cargo.get("gm_barcode") or "").strip()
            matches = cargo_candidates.get(gm_barcode.casefold()) or []
            if len(matches) != 1:
                continue
            box, delta = matches[0]
            box_code = str(box.get("box_code") or box.get("code") or "").strip()
            if len(box_candidates.get(box_code.casefold()) or []) == 1:
                resolved_pair = (cargo, box, delta)
                break
        if resolved_pair is None:
            break
        cargo, box, delta = resolved_pair
        gm_key = str(cargo.get("gm_barcode") or "").strip().casefold()
        box_key = str(box.get("box_code") or box.get("code") or "").strip().casefold()
        supplement_assignments[gm_key] = {
            "box": box,
            "delta": delta,
        }
        consumed_delivered_boxes.add(box_key)
        remaining_cargoes = [row for row in remaining_cargoes if row is not cargo]
        remaining_boxes = [row for row in remaining_boxes if row is not box]

    if remaining_boxes and not repack_all_delivered:
        errors.append(
            "Состав уже доставленных коробов не соответствует списку ШК ГМ Ozon. "
            "Требуется физическая переупаковка; автоматическая замена запрещена."
        )

    targets: list[dict] = []
    target_totals: Counter[str] = Counter()
    remaining_loose = Counter(loose_totals)

    def allocate_target_items(identity: str, quantity: int) -> list[dict]:
        allocated: list[dict] = []
        remaining = int(quantity)
        for source in loose_by_identity.get(identity) or []:
            available = int(source.get("remaining") or 0)
            if available <= 0:
                continue
            taken = min(available, remaining)
            loose_item = source["item"]
            allocated.append(
                {
                    "key": str(loose_item.get("key") or "").strip(),
                    "sku_code": str(loose_item.get("sku_code") or "").strip(),
                    "name": str(loose_item.get("name") or "").strip(),
                    "size": str(loose_item.get("size") or "").strip(),
                    "barcode": str(loose_item.get("barcode") or "").strip(),
                    "goods_type": str(loose_item.get("goods_type") or "").strip(),
                    "qty": taken,
                }
            )
            source["remaining"] = available - taken
            remaining -= taken
            if remaining <= 0:
                break
        return allocated if remaining == 0 else []

    for cargo_signature, cargoes in cargoes_by_signature.items():
        for cargo in cargoes:
            gm_barcode = str(cargo.get("gm_barcode") or "").strip()
            if gm_barcode.casefold() in consumed_gm:
                continue
            supplement = supplement_assignments.get(gm_barcode.casefold()) or {}
            target_signature = supplement.get("delta") or cargo_signature
            target_identities = {identity for identity, _quantity in target_signature}
            loose_target_identities = target_identities & set(loose_by_identity)
            if not loose_target_identities:
                # This unmatched GM belongs to stock that has not reached OTG yet.
                # It is handled by the independent pick/discrepancy workflow and
                # must not block packing loose units that are already physically here.
                continue
            if loose_target_identities != target_identities:
                errors.append(
                    f"ШК ГМ {gm_barcode} содержит товар без короба вместе с товаром, "
                    "который ещё не доставлен в OTG. Упаковка этого грузового места остановлена."
                )
                continue
            if any(
                int(remaining_loose.get(barcode) or 0) < int(quantity)
                for barcode, quantity in target_signature
            ):
                continue
            target_items: list[dict] = []
            for identity, quantity in target_signature:
                allocated_items = allocate_target_items(identity, int(quantity))
                if not allocated_items:
                    target_items = []
                    break
                target_items.extend(allocated_items)
                target_totals[identity] += int(quantity)
                remaining_loose[identity] -= int(quantity)
            if not target_items:
                continue
            targets.append(
                {
                    "gm_barcode": gm_barcode,
                    "supply_id": str(cargo.get("supply_id") or "").strip(),
                    "bundle_id": str(cargo.get("bundle_id") or "").strip(),
                    "existing_box_code": str(
                        (supplement.get("box") or {}).get("box_code")
                        or (supplement.get("box") or {}).get("code")
                        or ""
                    ).strip(),
                    "existing_qty": sum(
                        int(item.get("qty") or 0)
                        for item in (supplement.get("box") or {}).get("items") or []
                        if isinstance(item, dict)
                    ),
                    "ozon_qty": sum(quantity for _barcode, quantity in cargo_signature),
                    "qty": sum(int(item.get("qty") or 0) for item in target_items),
                    "items": target_items,
                }
            )

    if dict(target_totals) != dict(loose_totals):
        errors.append(
            "Остаток товара без короба не совпадает с нераспределёнными ШК ГМ Ozon. "
            "Короба и остатки не изменены."
        )

    ready = bool(targets) and not errors
    return {
        "required": True,
        "ready": ready,
        "targets": targets if ready else [],
        "errors": list(dict.fromkeys(errors)),
        "target_count": len(targets) if ready else 0,
    }


def _ozon_gm_repack_context(
    order: ShippingOrder,
    *,
    delivered_boxes: list[dict] | None = None,
) -> dict:
    """Describe the explicit OTG re-packing step when Ozon GM and Fullbox boxes differ."""
    default = {
        "required": False,
        "ready": False,
        "errors": [],
        "source_box_count": 0,
        "target_box_count": 0,
        "items": [],
        "gm_plan": {
            "required": False,
            "ready": True,
            "targets": [],
            "errors": [],
            "target_count": 0,
        },
    }
    if not _is_current_ozon_shipping(order) or order.status != ShippingOrder.STATUS_PICKING:
        return default

    from .packing import (
        _shipping_delivered_boxes,
        _shipping_loose_items,
        _shipping_repack_items,
    )
    from .truth import ShippingTruthService

    boxes = list(delivered_boxes) if delivered_boxes is not None else _shipping_delivered_boxes(order)
    target_box_count = max(int(getattr(order, "expected_boxes", 0) or 0), 0)
    source_box_count = len(boxes)
    if not boxes or target_box_count <= 0 or source_box_count == target_box_count:
        return default

    # A lower Fullbox box count is not automatically a repack of every box.
    # When the boxes already in OTG match Ozon cargoes exactly and the loose
    # balance can create precisely the missing GM boxes, keep those boxes as-is
    # and send only the loose balance through the dedicated packing screen.
    if source_box_count < target_box_count:
        loose_items = _shipping_loose_items(order)
        if loose_items:
            incremental_plan = _ozon_loose_packing_gm_plan(
                order,
                loose_items=loose_items,
            )
            new_box_count = sum(
                1
                for target in incremental_plan.get("targets") or []
                if not str(target.get("existing_box_code") or "").strip()
            )
            if (
                incremental_plan.get("ready")
                and source_box_count + new_box_count == target_box_count
            ):
                return default
    errors = list(_strict_ozon_gm_intake_errors(order))
    truth = ShippingTruthService.for_order(order)
    if truth.quantity_mismatch_rows:
        errors.append(
            "Сначала устраните расхождение по товарному ШК и количеству в OTG."
        )
    items = _shipping_repack_items(order)
    if not items:
        errors.append("Не найден состав исходных коробов в OTG для переупаковки.")
    gm_plan = _ozon_loose_packing_gm_plan(
        order,
        loose_items=items,
        repack_all_delivered=True,
    )
    errors.extend(gm_plan.get("errors") or [])
    if int(gm_plan.get("target_count") or 0) != target_box_count:
        errors.append(
            "Количество составов ШК ГМ Ozon не совпадает с количеством грузомест заявки."
        )
    errors = list(dict.fromkeys(str(error or "").strip() for error in errors if str(error or "").strip()))
    return {
        "required": True,
        "ready": bool(gm_plan.get("ready")) and not errors,
        "errors": errors,
        "source_box_count": source_box_count,
        "target_box_count": target_box_count,
        "items": items,
        "gm_plan": gm_plan,
    }


def _build_ozon_gm_discrepancy_panel(
    order: ShippingOrder,
    *,
    ozon_summary: dict,
    delivered_boxes: list[dict],
    discrepancy_context: dict,
) -> dict:
    if not ozon_summary.get("show"):
        return {"show": False}

    binding_result = validate_marketplace_item_binding(order, boxes=delivered_boxes)
    if not binding_result.get("applies") or binding_result.get("kind") != "ozon":
        return {"show": False}

    def _totals(source) -> dict[str, int]:
        return {
            str(barcode or "").strip(): _positive_int(qty)
            for barcode, qty in (source or {}).items()
            if str(barcode or "").strip()
        }

    request_totals = _totals(binding_result.get("request_totals"))
    fact_totals = _totals(binding_result.get("fact_totals"))
    gm_totals = _totals(binding_result.get("gm_totals"))
    gm_items_by_barcode: dict[str, dict] = {}
    for cargo in ozon_summary.get("gm_cargoes") or []:
        if not isinstance(cargo, dict):
            continue
        for item in cargo.get("items") or []:
            if not isinstance(item, dict):
                continue
            barcode = str(item.get("barcode") or "").strip()
            if not barcode:
                continue
            gm_items_by_barcode.setdefault(
                barcode,
                {
                    "barcode": barcode,
                    "offer_id": str(item.get("offer_id") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                },
            )

    mismatch_rows = list(marketplace_binding_mismatch_rows(binding_result))
    missing_request_rows: list[dict] = []
    seen_missing: set[str] = set()
    for barcode, ozon_qty in sorted(gm_totals.items(), key=lambda item: item[0].casefold()):
        if ozon_qty <= 0 or _positive_int(request_totals.get(barcode)) > 0:
            continue
        info = gm_items_by_barcode.get(barcode) or {}
        missing_request_rows.append(
            {
                "barcode": barcode,
                "offer_id": info.get("offer_id") or "—",
                "name": info.get("name") or "—",
                "ozon_qty": ozon_qty,
                "fullbox_qty": _positive_int(request_totals.get(barcode)),
                "fact_qty": _positive_int(fact_totals.get(barcode)),
            }
        )
        seen_missing.add(barcode.casefold())
    for row in mismatch_rows:
        barcode = str(row.get("barcode") or "").strip()
        if not barcode or barcode.casefold() in seen_missing:
            continue
        if _positive_int(row.get("marketplace_qty")) <= 0 or _positive_int(row.get("requested_qty")) > 0:
            continue
        missing_request_rows.append(
            {
                "barcode": barcode,
                "offer_id": (gm_items_by_barcode.get(barcode) or {}).get("offer_id") or "—",
                "name": (gm_items_by_barcode.get(barcode) or {}).get("name") or "—",
                "ozon_qty": _positive_int(row.get("marketplace_qty")),
                "fullbox_qty": _positive_int(row.get("requested_qty")),
                "fact_qty": _positive_int(row.get("fact_qty")),
            }
        )

    ozon_box_count = (
        _positive_int(ozon_summary.get("boxes_total"))
        or len(ozon_summary.get("gm_cargoes") or [])
        or len(ozon_summary.get("gm_barcodes") or [])
    )
    fullbox_box_count = _positive_int(getattr(order, "expected_boxes", 0))
    fact_box_count = len(delivered_boxes or [])
    additional_pick_move_ids = list(discrepancy_context.get("additional_pick_move_ids") or [])
    added_box_codes = list(discrepancy_context.get("added_box_codes") or [])
    resolved_by_supplement = bool(
        discrepancy_context.get("status") == "resolved"
        and discrepancy_context.get("decision") == "supplement"
    )
    supplement_closed = bool(
        resolved_by_supplement
        or discrepancy_context.get("is_resolved")
        and (
            discrepancy_context.get("decision") == "supplement"
            or discrepancy_context.get("supplement_fact_complete")
            or additional_pick_move_ids
        )
    )
    if supplement_closed:
        discrepancy_state_label = "Добор закрыт через расхождение"
    elif discrepancy_context.get("supplement_fact_complete"):
        discrepancy_state_label = "Добор подтвержден сканами"
    elif discrepancy_context.get("pickup_status_label"):
        discrepancy_state_label = str(discrepancy_context.get("pickup_status_label") or "")
    elif discrepancy_context.get("has_history"):
        discrepancy_state_label = str(
            discrepancy_context.get("client_summary_title")
            or discrepancy_context.get("summary_title")
            or "Расхождение оформлено"
        )
    else:
        discrepancy_state_label = "Расхождение не оформлялось"

    active_missing_request_rows = [] if supplement_closed else missing_request_rows
    resolved_request_rows = missing_request_rows if supplement_closed else []
    active_mismatch_rows = [] if supplement_closed else mismatch_rows

    return {
        "show": bool(
            ozon_box_count
            or fullbox_box_count
            or fact_box_count
            or active_missing_request_rows
            or resolved_request_rows
            or active_mismatch_rows
            or discrepancy_context.get("has_history")
        ),
        "resolved_by_supplement": resolved_by_supplement,
        "ozon_box_count": ozon_box_count,
        "fullbox_box_count": fullbox_box_count,
        "fact_box_count": fact_box_count,
        "gm_count": len(ozon_summary.get("gm_cargoes") or ozon_summary.get("gm_barcodes") or []),
        "gm_matched_count": _positive_int(ozon_summary.get("gm_matched_count")),
        "gm_unassigned_count": _positive_int(ozon_summary.get("gm_unassigned_count")),
        "box_count_mismatch": bool(ozon_box_count and fullbox_box_count and ozon_box_count != fullbox_box_count),
        "missing_request_rows": active_missing_request_rows,
        "resolved_request_rows": resolved_request_rows,
        "mismatch_rows": active_mismatch_rows,
        "resolved_mismatch_rows": mismatch_rows if supplement_closed else [],
        "discrepancy_state_label": discrepancy_state_label,
        "discrepancy_closed": bool(discrepancy_context.get("is_resolved")),
        "supplement_closed": supplement_closed,
        "additional_pick_move_ids": additional_pick_move_ids,
        "added_box_codes": added_box_codes,
        "ready_for_palletization": bool(discrepancy_context.get("ready_for_palletization")),
    }


def _storekeeper_marketplace_binding_alert(
    order: ShippingOrder,
    *,
    boxes: list[dict] | None,
) -> dict:
    empty = {
        "show": False,
        "blocking": False,
        "marketplace_label": "",
        "rows": [],
    }
    if not boxes:
        return empty
    try:
        result = validate_marketplace_item_binding(order, boxes=boxes)
    except Exception:
        logger.exception(
            "Could not build marketplace quantity alert for shipping order %s",
            getattr(order, "pk", None),
        )
        return empty
    if not result.get("applies") or result.get("kind") != "ozon":
        return empty

    rows = marketplace_binding_mismatch_rows(result)
    supplement_complete = shipping_supplement_fact_is_complete(order, result)
    discrepancy_payload = (
        order.shipping_discrepancy_payload
        if isinstance(getattr(order, "shipping_discrepancy_payload", None), dict)
        else {}
    )
    resolved_with_supplement = bool(
        str(getattr(order, "shipping_discrepancy_status", "") or "").strip() == "resolved"
        and str(discrepancy_payload.get("decision") or "").strip() == "supplement"
    )
    closed_by_discrepancy = bool(rows and (supplement_complete or resolved_with_supplement))
    gate_errors = (
        enforced_marketplace_item_binding_errors(order, boxes=boxes)
        if rows and not closed_by_discrepancy and should_enforce_marketplace_item_binding(order)
        else []
    )

    return {
        "show": bool(rows and not closed_by_discrepancy),
        "blocking": bool(gate_errors and not closed_by_discrepancy),
        "closed_by_discrepancy": closed_by_discrepancy,
        "resolved_with_supplement": resolved_with_supplement,
        "supplement_complete": supplement_complete,
        "approved_with_discrepancy": bool(
            rows
            and not closed_by_discrepancy
            and should_enforce_marketplace_item_binding(order)
            and not gate_errors
            and not result.get("ready")
        ),
        "marketplace_label": "Ozon",
        "rows": rows,
    }


def _ozon_gm_codes_by_item(meta: dict) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    for row in meta.get("composition") or []:
        if not isinstance(row, dict):
            continue
        codes = _as_clean_list(row.get("ozon_gm_barcodes"))
        if not codes:
            continue
        barcode = str(row.get("barcode") or "").strip()
        offer_id = str(row.get("offer_id") or row.get("sku_code") or "").strip()
        for key in ((barcode, ""), ("", offer_id), (barcode, offer_id)):
            if any(key):
                result[key] = list(dict.fromkeys([*result.get(key, []), *codes]))
    if not result and len(meta.get("composition") or []) == 1 and meta.get("gm_barcodes"):
        row = meta["composition"][0]
        if isinstance(row, dict):
            barcode = str(row.get("barcode") or "").strip()
            offer_id = str(row.get("offer_id") or row.get("sku_code") or "").strip()
            for key in ((barcode, ""), ("", offer_id), (barcode, offer_id)):
                if any(key):
                    result[key] = list(meta["gm_barcodes"])
    return result


def _attach_ozon_gm_to_selected_rows(rows: list[dict], meta: dict) -> list[dict]:
    # ШК ГМ относится к cargo Ozon, а не к строке товара. Не распределяем коды
    # последовательно или по косвенным признакам: точная структура хранится в audit payload.
    return [dict(row) for row in rows]


@transaction.atomic
def reserve_order(
    order: ShippingOrder,
    user=None,
    *,
    target_status: str | None = ShippingOrder.STATUS_RESERVED,
    log_description: str = "Резерв подтвержден",
    ozon_api_meta: dict | None = None,
) -> None:
    if order.is_closed():
        raise ValidationError("Нельзя резервировать закрытую заявку.")
    items = list(order.items.order_by("id"))
    if not items:
        raise ValidationError("В заявке нет позиций для резерва.")

    binding_errors = enforced_ozon_request_binding_errors(
        order,
        gm_cargoes=(ozon_api_meta or {}).get("gm_cargoes") if ozon_api_meta is not None else None,
        expected_boxes=order.expected_boxes,
        ozon_boxes_total=(ozon_api_meta or {}).get("boxes_total") if ozon_api_meta is not None else None,
        compare_source_box_count=False,
    )
    if binding_errors:
        raise ValidationError(
            [
                "Нельзя подтвердить резерв: исправьте состав заявки по данным ШК ГМ Ozon.",
                *binding_errors,
            ]
        )

    availability: dict[tuple[str, ...], int] = defaultdict(int)
    for row in shipping_available_items(order, exclude_order=order):
        availability[_key(row.get("sku"), row.get("size"), row.get("goods_type"), row.get("barcode"))] += int(row.get("qty") or 0)

    shortage_rows: list[tuple[ShippingOrderItem, int, int, tuple[str, ...]]] = []
    for item in items:
        needed = int(item.qty_requested or 0)
        key = _key(item.sku_code, item.size, item.goods_type, item.barcode)
        available = int(availability.get(key, 0))
        if available < needed:
            shortage_rows.append((item, needed, available, key))
            continue
        availability[key] = available - needed

    if shortage_rows:
        blockers_by_item = _shipping_reserve_blockers_by_item(order, items)
        shortages = [
            _format_shipping_reserve_shortage(
                item,
                needed=needed,
                available=available,
                blockers=blockers_by_item.get(key),
            )
            for item, needed, available, key in shortage_rows
        ]
        raise ValidationError("Недостаточно товара для резерва: " + " ".join(shortages))

    _assert_pool_reserve_capacity_locked(order, items)
    matching_pool_reserve = _matching_pool_shipping_reserve_exists_locked(order, items)
    if not matching_pool_reserve:
        try:
            _assert_selected_box_reserve_capacity_locked(order, items)
        except ValidationError:
            if not _is_current_ozon_shipping(order):
                raise
            _reselect_selected_boxes_locked(order, items)
            # Always re-run the original guard. If the current free remainder
            # is still insufficient, no reserve or warehouse stock is changed.
            _assert_selected_box_reserve_capacity_locked(order, items)
    warehouse_items: list[dict] = []
    for item in items:
        qty = int(item.qty_requested or 0)
        if qty <= 0:
            item.qty_reserved = 0
            item.save(update_fields=["qty_reserved", "updated_at"])
            continue
        warehouse_items.append(
            {
                "sku": item.sku_code,
                "sku_code": item.sku_code,
                "size": item.size,
                "barcode": item.barcode,
                "goods_type": item.goods_type,
                "qty": qty,
                "box_codes": _box_codes_from_item_comment(item.comment),
                "reserve_pool": True,
            }
        )
        item.qty_reserved = qty
        item.save(update_fields=["qty_reserved", "updated_at"])
    if not matching_pool_reserve:
        try:
            WarehouseWritePathService.replace_shipping_reserves(
                agency=order.agency,
                order_id=order.number,
                items=warehouse_items,
                created_by=user if getattr(user, "is_authenticated", False) else None,
                source_document_type="shipping_order",
                source_document_id=order.number,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    update_fields = ["reserved_at", "updated_at"]
    if target_status is not None and order.status != target_status:
        order.status = target_status
        update_fields.append("status")
    order.reserved_at = timezone.now()
    order.save(update_fields=update_fields)
    _log_order(order, action="status", user=user, description=log_description)


@transaction.atomic
def release_order_reserves(
    order: ShippingOrder,
    user=None,
    *,
    reset_workflow_status: bool = True,
) -> None:
    """Release shipping reserves via existing warehouse API (no warehouse code changes)."""
    if order.is_closed():
        raise ValidationError("Нельзя снять резерв с закрытой заявки.")
    WarehouseWritePathService.replace_shipping_reserves(
        agency=order.agency,
        order_id=order.number,
        items=[],
        created_by=user if getattr(user, "is_authenticated", False) else None,
        source_document_type="shipping_order",
        source_document_id=order.number,
    )
    order.items.update(qty_reserved=0)
    update_fields: list[str] = []
    if order.reserved_at is not None:
        order.reserved_at = None
        update_fields.append("reserved_at")
    if reset_workflow_status and order.status in {
        ShippingOrder.STATUS_RESERVED,
        ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        ShippingOrder.STATUS_PICKING,
        ShippingOrder.STATUS_PACKED,
    }:
        order.status = ShippingOrder.STATUS_SUBMITTED
        update_fields.append("status")
    if update_fields:
        update_fields.append("updated_at")
        order.save(update_fields=update_fields)
    _log_order(order, action="status", user=user, description="Резерв снят")


def apply_client_shipping_reserve_on_submit(
    order: ShippingOrder,
    user=None,
    *,
    ozon_api_meta: dict | None = None,
) -> None:
    """Client LK: reserve stock only when the request is sent to the manager."""
    if order.status != ShippingOrder.STATUS_SUBMITTED:
        return
    reserve_order(
        order,
        user,
        target_status=ShippingOrder.STATUS_SUBMITTED,
        log_description="Резерв при отправке заявки менеджеру (ЛК клиента)",
        ozon_api_meta=ozon_api_meta,
    )


def create_ozon_api_shipping_orders_batch(
    *,
    agency: Agency,
    user,
    payloads: list[dict],
    stock_rows: list[dict],
    ship_date: date,
    supply_type: str = ShippingOrder.SUPPLY_BOX,
    vehicle_type: str = ShippingOrder.VEHICLE_FULFILLMENT,
    comment: str = "",
) -> list[ShippingOrder]:
    """Create exact whole-box Ozon shipments through the existing reserve API.

    No warehouse model or transition is implemented here. Every created order
    goes through the same reserve service and manager task used by the normal
    client form. Non-multiple quantities must be opened in the existing single
    form and completed through its piece/box-split flow.
    """
    from .ozon_template import items_from_selected_boxes

    if not payloads:
        raise ValidationError("Нет готовых поставок Ozon для отправки.")
    if len(payloads) > 10:
        raise ValidationError("За один раз можно отправить не более 10 поставок Ozon.")
    if supply_type not in dict(ShippingOrder.SUPPLY_TYPE_CHOICES):
        raise ValidationError("Выберите корректный тип поставки.")
    if vehicle_type not in dict(ShippingOrder.VEHICLE_TYPE_CHOICES):
        raise ValidationError("Выберите корректный транспорт.")
    if ship_date < timezone.localdate():
        raise ValidationError("Дата отгрузки со склада не может быть задним числом.")

    market = (
        Market.objects.filter(name__iexact="Ozon").first()
        or Market.objects.filter(name__icontains="Ozon").first()
    )
    if market is None:
        raise ValidationError("Маркетплейс Ozon не найден в справочнике.")

    created: list[ShippingOrder] = []
    with transaction.atomic():
        Agency.objects.select_for_update().filter(pk=agency.pk).only("id").first()
        for payload in payloads:
            if payload.get("already_uploaded"):
                existing = payload.get("existing_shipping") or {}
                raise ValidationError(
                    f"Поставка Ozon {payload.get('order_number') or payload.get('order_id')} "
                    f"уже загружена в {existing.get('number') or 'Fullbox'}."
                )
            if payload.get("is_multi_destination"):
                raise ValidationError(
                    f"Поставка Ozon {payload.get('order_number') or payload.get('order_id')} "
                    "имеет несколько конечных складов и требует отдельного распределения."
                )
            if (payload.get("stock_match") or {}).get("status") != "full":
                raise ValidationError(
                    f"Поставка Ozon {payload.get('order_number') or payload.get('order_id')} "
                    "не набрана полностью целыми коробами. Откройте её для поштучного подбора."
                )

            selected_rows, box_count, item_errors = items_from_selected_boxes(
                stock_rows,
                payload.get("selected_boxes") or {},
            )
            if item_errors:
                raise ValidationError(item_errors)
            if not selected_rows or box_count <= 0:
                raise ValidationError("В поставке Ozon не выбраны доступные короба Fullbox.")
            requested_qty = sum(int(row.get("quantity") or 0) for row in payload.get("composition") or [])
            selected_qty = sum(int(row.get("qty_requested") or 0) for row in selected_rows)
            if requested_qty <= 0 or selected_qty != requested_qty:
                raise ValidationError(
                    f"Поставка Ozon {payload.get('order_number') or payload.get('order_id')}: "
                    f"требуется {requested_qty} шт., целыми коробами выбрано {selected_qty} шт."
                )

            form_data = payload.get("form") or {}
            slot_date_value = str(form_data.get("slot_date") or "").strip()
            slot_time_value = str(form_data.get("slot_time") or "").strip()
            try:
                slot_date = datetime.strptime(slot_date_value, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                raise ValidationError("Ozon не вернул дату слота для одной из поставок.")
            slot_time = None
            if slot_time_value:
                for fmt in ("%H:%M:%S", "%H:%M"):
                    try:
                        slot_time = datetime.strptime(slot_time_value, fmt).time()
                        break
                    except ValueError:
                        continue

            destination = str(form_data.get("destination_warehouse") or "").strip()
            transit_address = str(form_data.get("transit_address") or "").strip()
            if not destination:
                destinations = [str(value or "").strip() for value in payload.get("destination_warehouses") or []]
                destination = next((value for value in destinations if value), "")
            if not destination:
                raise ValidationError(
                    f"Поставка Ozon {payload.get('order_number') or payload.get('order_id')}: "
                    "не получен склад назначения."
                )

            gm_barcodes = [str(value or "").strip() for value in payload.get("gm_barcodes") or [] if str(value or "").strip()]
            supply_number = str(payload.get("order_number") or form_data.get("supply_number") or "").strip()
            shipping_barcode = str(form_data.get("shipping_barcode") or "").strip() or supply_number
            meta = {
                "payload": payload,
                "order_id": str(payload.get("order_id") or ""),
                "order_number": supply_number,
                "supply_number": supply_number,
                "shipping_barcode": shipping_barcode,
                "gm_barcodes": gm_barcodes,
                "gm_cargoes": list(payload.get("gm_cargoes") or []),
                "boxes_total": int(payload.get("ozon_boxes_total") or len(gm_barcodes) or 0),
                "composition": list(payload.get("composition") or []),
                "destinations": list(payload.get("destination_warehouses") or []),
            }
            eta_at = timezone.make_aware(
                datetime.combine(ship_date, time(hour=12)),
                timezone.get_current_timezone(),
            )
            order = ShippingOrder(
                number=next_shipping_number(),
                agency=agency,
                created_by=user if getattr(user, "is_authenticated", False) else None,
                status=ShippingOrder.STATUS_SUBMITTED,
                delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
                marketplace=market,
                slot_date=slot_date,
                slot_time=slot_time,
                planned_ship_date=ship_date,
                eta_at=eta_at,
                shipping_barcode=shipping_barcode,
                supply_number=supply_number,
                wb_supply_barcode="",
                wb_transit_warehouse=bool(form_data.get("wb_transit_warehouse")),
                transit_address=transit_address,
                destination_warehouse=destination,
                supply_type=supply_type,
                vehicle_type=vehicle_type,
                expected_boxes=int(box_count),
                comment=_comment_with_ozon_api_meta(comment, meta),
            )
            duplicate = _find_active_ozon_supply_duplicate_for_update(order)
            if duplicate is not None:
                raise ValidationError(
                    f"Поставка Ozon {supply_number} уже загружена в заявку "
                    f"{duplicate.number}. Дубль не создан."
                )
            order.save()
            for row in selected_rows:
                ShippingOrderItem.objects.create(order=order, **row)

            # ШК ГМ Ozon are cargo identifiers, not Fullbox box identifiers.
            # Keep the exact Ozon structure in audit/comment, but do not invent a
            # sequential GM-to-Fullbox-box binding in warehouse reserve checks.
            reserve_meta = dict(meta)
            reserve_meta["gm_cargoes"] = []
            apply_client_shipping_reserve_on_submit(order, user, ozon_api_meta=reserve_meta)
            ensure_manager_review_task(order, user)
            ozon_snapshot = _build_ozon_api_snapshot_payload(meta)
            log_order_action(
                action="create",
                order_id=order.number,
                order_type="shipping",
                user=user,
                agency=agency,
                description="Клиент отправил поставку Ozon из массового выбора",
                payload=order_payload(
                    order,
                    extra={
                        **ozon_snapshot,
                        "ozon_order_id": meta["order_id"],
                        "ozon_order_number": supply_number,
                        "ozon_gm_barcodes": gm_barcodes,
                        "mass_ozon_submit": True,
                        "warehouse_ship_date": ship_date.isoformat(),
                    },
                ),
            )
            created.append(order)
    return created


def release_client_shipping_reserve_on_cancel(order: ShippingOrder, user=None) -> None:
    """Client LK: unreserve when client cancels / returns request before approval."""
    release_order_reserves(order, user, reset_workflow_status=False)


_OTG_PICK_WAIT_REASON_PREFIXES = (
    "Паллета занята заданием ",
    "Паллеты заняты заданиями ",
    "Паллеты с зарезервированным товаром уже заняты ",
    "Паллета закреплена за более приоритетной заявкой ",
    "Паллеты закреплены за более приоритетными заявками ",
    "Нельзя переназначить занятый исходный короб ",
)


def _otg_pick_waits_for_stock(reason: str) -> bool:
    return str(reason or "").strip().startswith(_OTG_PICK_WAIT_REASON_PREFIXES)


@transaction.atomic
def return_order_to_draft(order: ShippingOrder, user=None) -> None:
    if order.is_closed():
        raise ValidationError("Нельзя вернуть закрытую заявку в подготовку.")
    release_order_reserves(order, user)
    order.status = ShippingOrder.STATUS_DRAFT
    order.reserved_at = None
    order.save(update_fields=["status", "reserved_at", "updated_at"])
    close_manager_review_task(order)
    close_storekeeper_task(order)
    close_logistician_task(order)
    close_shipping_act_manager_task(order)
    _log_order(
        order,
        action="status",
        user=user,
        description="Заявка возвращена в подготовку клиентом",
    )


@transaction.atomic
def create_pick_tasks(order: ShippingOrder, user=None, *, requested_by_name: str = "", requested_by_role: str = "") -> list[str]:
    order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    if order.is_closed():
        raise ValidationError("Нельзя создать задания ричтраку для закрытой заявки.")
    use_otg_planner = bool(getattr(settings, "USE_OTG_REACHTRUCK_PLANNER", False))
    if use_otg_planner:
        # Box codes are stored on individual shipping items, not in a unique
        # database reservation. Serialize OTG dispatch per client so two orders
        # cannot rebind to the same free box at the same moment.
        Agency.objects.select_for_update().filter(pk=order.agency_id).only("id").first()

        from otg_reachtruck.services import (
            ensure_waiting_otg_shipping_pick_request,
            rebind_unavailable_shipping_source_boxes,
            shipping_queue_priority_wait_reason,
        )

        priority_wait_reason = shipping_queue_priority_wait_reason(order)
        if priority_wait_reason:
            if order.status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
                ensure_waiting_otg_shipping_pick_request(
                    order=order,
                    user=user,
                    requested_by_name=requested_by_name,
                    requested_by_role=requested_by_role,
                    waiting_message=priority_wait_reason,
                )
                return []
            raise ValidationError(priority_wait_reason)

        try:
            box_replacements = rebind_unavailable_shipping_source_boxes(order)
        except (ValidationError, ValueError) as exc:
            reason = (
                "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else str(exc)
            ).strip()
            if (
                order.status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED
                and _otg_pick_waits_for_stock(reason)
            ):
                ensure_waiting_otg_shipping_pick_request(
                    order=order,
                    user=user,
                    requested_by_name=requested_by_name,
                    requested_by_role=requested_by_role,
                    waiting_message=reason,
                )
                return []
            raise
        if box_replacements:
            _log_order(
                order,
                action="correction",
                user=user,
                description="Недоступные короба автоматически заменены перед созданием заданий ричтраку",
                extra={"auto_source_box_replacements": box_replacements},
            )
    pick_readiness = shipping_pick_readiness(order)
    if not pick_readiness["can_pick"]:
        reason = str(pick_readiness["reason"] or "").strip()
        if (
            _otg_pick_waits_for_stock(reason)
            and order.status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED
        ):
            if use_otg_planner:
                from otg_reachtruck.services import ensure_waiting_otg_shipping_pick_request

                ensure_waiting_otg_shipping_pick_request(
                    order=order,
                    user=user,
                    requested_by_name=requested_by_name,
                    requested_by_role=requested_by_role,
                    waiting_message=reason,
                )
            return []
        raise ValidationError(reason or "Не удалось сформировать задания ричтраку.")
    try:
        if use_otg_planner:
            from otg_reachtruck.services import create_otg_shipping_pick_request

            _move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
                order=order,
                user=user,
                requested_by_name=requested_by_name,
                requested_by_role=requested_by_role,
                allow_partial=False,
            )
        else:
            _move_request, move_ids, shortage_qty = create_shipping_pick_request(
                order=order,
                user=user,
                requested_by_name=requested_by_name,
                requested_by_role=requested_by_role,
            )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if not move_ids and shortage_qty > 0:
        if use_otg_planner:
            raise ValidationError(
                "Нельзя создать полный подбор: "
                f"не хватает {int(shortage_qty or 0)} шт. по заявке."
            )
        raise ValidationError("Не удалось сформировать задания ричтраку: не найден доступный товар по паллетам.")
    if move_ids:
        order.status = ShippingOrder.STATUS_PICKING
        order.save(update_fields=["status", "updated_at"])
    elif shortage_qty == 0:
        order.status = ShippingOrder.STATUS_PICKING
        order.save(update_fields=["status", "updated_at"])
        _log_order(
            order,
            action="status",
            user=user,
            description="Товар уже доставлен в OTG. Можно переходить к паллетизации.",
            extra={"reachtruck_move_ids": [], "reachtruck_shortage_qty": 0},
        )
        return []
    description = "Созданы задания ричтраку на отбор в OTG"
    if shortage_qty > 0:
        description += f" (частично, дефицит {shortage_qty} шт.)"
    _log_order(
        order,
        action="status",
        user=user,
        description=description,
        extra={"reachtruck_move_ids": move_ids, "reachtruck_shortage_qty": shortage_qty},
    )
    return move_ids


def shipping_pick_readiness(order: ShippingOrder) -> dict[str, str | bool]:
    if order.is_closed():
        return {"can_pick": False, "reason": "Заявка уже закрыта."}

    items = list(order.items.order_by("id"))
    if not items:
        return {"can_pick": False, "reason": "В заявке нет позиций для отбора."}

    ozon_intake_errors = _strict_ozon_gm_intake_errors(order)
    if ozon_intake_errors:
        details = "; ".join(ozon_intake_errors[:4])
        return {
            "can_pick": False,
            "reason": (
                "Нельзя начать отбор: сохранённые данные Ozon неполные или не совпадают "
                f"с заявкой Fullbox. {details} Остатки и задания не изменены."
            ),
        }

    from .selectors import shipping_otg_delivery_completed, shipping_otg_tasks_progress

    otg_progress = shipping_otg_tasks_progress(order)
    if int(otg_progress.get("active") or 0) > 0:
        total = int(otg_progress.get("total") or 0)
        active = int(otg_progress.get("active") or 0)
        return {
            "can_pick": False,
            "reason": f"Задания ричтраку уже созданы: в работе {active} из {total}.",
        }

    if shipping_otg_delivery_completed(order):
        return {
            "can_pick": False,
            "reason": "Товар уже доставлен в OTG. Можно переходить к паллетизации.",
        }

    binding_errors = enforced_ozon_request_binding_errors(
        order,
        expected_boxes=order.expected_boxes,
        compare_source_box_count=False,
    )
    if binding_errors:
        details = "; ".join(str(error or "").strip() for error in binding_errors[:3] if str(error or "").strip())
        return {
            "can_pick": False,
            "reason": (
                "Нельзя начать отбор: состав заявки не совпадает с ШК ГМ Ozon. "
                f"{details} Остатки и задания не изменены."
            ),
        }

    if getattr(settings, "USE_OTG_REACHTRUCK_PLANNER", False):
        from otg_reachtruck.services import (
            get_otg_confirmed_shortage,
            preview_otg_shipping_pick_coverage,
            shipping_active_source_wait_reason,
            shipping_queue_priority_wait_reason,
        )

        priority_wait_reason = shipping_queue_priority_wait_reason(order)
        if priority_wait_reason:
            return {"can_pick": False, "reason": priority_wait_reason}

        confirmed_shortage = get_otg_confirmed_shortage(order)
        if int(confirmed_shortage.get("shortage_boxes") or 0) > 0:
            return {
                "can_pick": False,
                "reason": (
                    "После «Нет на месте» разрешен только добор недостающих коробов. "
                    "Используйте действие «Создать добор»."
                ),
            }
        try:
            coverage = preview_otg_shipping_pick_coverage(order)
        except (ValidationError, ValueError) as exc:
            return {"can_pick": False, "reason": str(exc)}
        if not coverage.get("can_cover"):
            blocked_by_shipping_orders = [
                str(number or "").strip()
                for number in coverage.get("blocked_by_shipping_orders") or []
                if str(number or "").strip()
            ]
            if len(blocked_by_shipping_orders) == 1:
                return {
                    "can_pick": False,
                    "reason": (
                        f"Паллета занята заданием {blocked_by_shipping_orders[0]}, "
                        "дождитесь завершения."
                    ),
                }
            if blocked_by_shipping_orders:
                return {
                    "can_pick": False,
                    "reason": (
                        "Паллеты заняты заданиями "
                        f"{', '.join(blocked_by_shipping_orders)}, дождитесь завершения."
                    ),
                }
            active_source_wait_reason = shipping_active_source_wait_reason(order)
            if active_source_wait_reason:
                return {"can_pick": False, "reason": active_source_wait_reason}
            shortage_boxes = int(coverage.get("shortage_boxes") or 0)
            shortage_qty = int(coverage.get("shortage_qty") or 0)
            shortage_label = []
            if shortage_boxes > 0:
                shortage_label.append(f"{shortage_boxes} короб.")
            if shortage_qty > 0:
                shortage_label.append(f"{shortage_qty} шт.")
            details = " / ".join(shortage_label) or "недостаточно доступного товара"
            return {
                "can_pick": False,
                "reason": (
                    "Нельзя создать полный подбор: "
                    f"не хватает {details}. Остатки и заявка не изменены."
                ),
            }
        return {"can_pick": True, "reason": ""}

    base_rows = snapshot_stock_rows(agency=order.agency)
    if not base_rows:
        return {"can_pick": False, "reason": "На складе нет остатков клиента для отбора."}

    blocked_pallets = {
        str(task.pallet_code or "").strip()
        for task in MoveTask.objects.select_related("request")
        .filter(
            request__agency=order.agency,
            status=MoveTask.STATUS_IN_PROGRESS,
        )
        .exclude(pallet_code="")
        if str(task.pallet_code or "").strip()
    }
    blocked_pallets.update(active_pallet_lock_codes(agency_id=order.agency_id))

    has_matching_stock = False
    has_matching_pallet_stock = False
    has_unblocked_pallet_stock = False

    for item in items:
        qty_required = max(int(item.qty_reserved or item.qty_requested or 0), 0)
        if qty_required <= 0:
            continue
        sku_value = str(item.sku_code or "").strip()
        size_value = str(item.size or "").strip().lower()
        barcode_value = str(item.barcode or "").strip().lower()
        goods_type_value = StockAvailabilityService.normalize_goods_type(item.goods_type)
        matching_rows = []
        for row in base_rows:
            row_barcode = str(row.get("barcode") or "").strip().lower()
            if barcode_value:
                if row_barcode != barcode_value:
                    continue
            else:
                if str(row.get("sku") or "").strip().lower() != sku_value.lower():
                    continue
                if size_value:
                    if str(row.get("size") or "").strip().lower() != size_value:
                        continue
                elif str(row.get("size") or "").strip():
                    continue
            row_goods_type = StockAvailabilityService.normalize_goods_type(row.get("goods_type"))
            if goods_type_value and row_goods_type and row_goods_type != goods_type_value:
                continue
            if int(row.get("qty") or 0) <= 0:
                continue
            matching_rows.append(row)
        if not matching_rows:
            continue
        has_matching_stock = True
        pallet_matching_rows = [row for row in matching_rows if str(row.get("pallet_code") or "").strip()]
        if not pallet_matching_rows:
            continue
        has_matching_pallet_stock = True
        if any(str(row.get("pallet_code") or "").strip() not in blocked_pallets for row in pallet_matching_rows):
            has_unblocked_pallet_stock = True
            break

    if not has_matching_stock:
        return {
            "can_pick": False,
            "reason": "Для зарезервированных позиций не найдено складских остатков.",
        }
    if not has_matching_pallet_stock:
        return {
            "can_pick": False,
            "reason": "Нельзя дать задание ричтраку: зарезервированный товар не размещен на паллетах.",
        }
    if not has_unblocked_pallet_stock:
        return {
            "can_pick": False,
            "reason": "Паллеты с зарезервированным товаром уже заняты в активных заданиях ричтрака.",
        }
    return {"can_pick": True, "reason": ""}


_SHIPPING_PROBLEM_REPORT_ROLES = {"manager", "head_manager", "director", "admin", "developer"}
_SHIPPING_PROBLEM_REPORT_ACTIVE_STATUSES = {
    ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
    ShippingOrder.STATUS_PICKING,
    ShippingOrder.STATUS_PACKED,
}
_SHIPPING_PROBLEM_REPORT_OPEN_DISCREPANCIES = {
    "pending",
    "pick_confirmation",
    "pickup_required",
}
_SHIPPING_PROBLEM_REPORT_DONE_STATUSES = {
    ShippingOrder.STATUS_SHIPPED,
    ShippingOrder.STATUS_PARTIAL,
    ShippingOrder.STATUS_CANCELED,
}


def _shipping_problem_report_context(
    *,
    scope: str,
    role: str | None,
    selected_client=None,
    limit: int = 8,
) -> dict:
    empty = {
        "show": False,
        "date_label": "",
        "has_rows": False,
        "sections": [],
        "totals": {
            "active_blockers": 0,
            "box_imbalance": 0,
            "missing_fact": 0,
            "reachtruck_errors": 0,
        },
    }
    if scope != "staff" or role not in _SHIPPING_PROBLEM_REPORT_ROLES:
        return empty

    today = _local_date()
    current_tz = timezone.get_current_timezone()
    start_at = datetime.combine(today, time.min)
    if timezone.is_naive(start_at):
        start_at = timezone.make_aware(start_at, current_tz)
    end_at = start_at + timedelta(days=1)

    related_names = {f.name for f in ShippingOrder._meta.get_fields()}
    qs = ShippingOrder.objects.select_related("agency", "marketplace").prefetch_related("items")
    if "parent" in related_names:
        qs = qs.filter(parent__isnull=True)
    if selected_client is not None:
        qs = qs.filter(agency=selected_client)
    elif role == "manager":
        qs = qs.filter(agency__in=_staff_client_queryset_for_role(role))

    qs = (
        qs.filter(
            Q(created_at__gte=start_at, created_at__lt=end_at)
            | Q(planned_ship_date=today)
            | Q(slot_date=today)
            | Q(status__in=_SHIPPING_PROBLEM_REPORT_ACTIVE_STATUSES)
            | Q(shipping_discrepancy_status__in=_SHIPPING_PROBLEM_REPORT_OPEN_DISCREPANCIES)
        )
        .exclude(status__in=_SHIPPING_PROBLEM_REPORT_DONE_STATUSES)
        .order_by("-planned_ship_date", "-slot_date", "-created_at", "-id")[:120]
    )
    orders = list(qs)
    if not orders:
        empty["show"] = True
        empty["date_label"] = today.strftime("%d.%m.%Y")
        empty["sections"] = [
            {"key": "active_blockers", "title": "Активные блокеры", "count": 0, "rows": [], "omitted": 0},
            {"key": "box_imbalance", "title": "Ozon/Fullbox не в балансе", "count": 0, "rows": [], "omitted": 0},
            {"key": "missing_fact", "title": "Недостающий факт в OTG", "count": 0, "rows": [], "omitted": 0},
            {"key": "reachtruck_errors", "title": "Ошибки ричтрака", "count": 0, "rows": [], "omitted": 0},
        ]
        return empty

    order_by_id = {int(order.pk): order for order in orders}
    order_by_id_text = {str(order.pk): order for order in orders}
    order_numbers = [str(order.number or "").strip() for order in orders if str(order.number or "").strip()]

    fact_counts_by_number: dict[str, int] = {}
    if order_numbers:
        fact_rows = (
            WarehouseStockSnapshot.objects.filter(
                is_archived=False,
                last_event__stock_context_type="shipping",
                last_event__stock_context_id__in=order_numbers,
                warehouse_state_code__in=[
                    WarehouseStateCode.IN_OTG.value,
                    WarehouseStateCode.PALLETIZING.value,
                    WarehouseStateCode.READY_FOR_LOADING.value,
                ],
            )
            .exclude(container_code="")
            .values("last_event__stock_context_id")
            .annotate(box_count=Count("container_code", distinct=True))
        )
        fact_counts_by_number = {
            str(row.get("last_event__stock_context_id") or "").strip(): int(row.get("box_count") or 0)
            for row in fact_rows
        }

    reachtruck_errors_by_order_id: dict[int, list[str]] = defaultdict(list)
    order_id_texts = list(order_by_id_text)
    if order_id_texts:
        request_rows = (
            MoveRequest.objects.filter(
                context_type=MoveRequest.CONTEXT_MANUAL,
                context_id__in=order_id_texts,
                destination_zone="OTG",
                updated_at__gte=start_at,
                updated_at__lt=end_at,
            )
            .exclude(status__in=[MoveRequest.STATUS_DONE, MoveRequest.STATUS_CANCELED])
            .filter(Q(status=MoveRequest.STATUS_BLOCKED) | ~Q(planning_error=""))
            .values("id", "context_id", "status", "planning_error")
        )
        for row in request_rows:
            order = order_by_id_text.get(str(row.get("context_id") or "").strip())
            if not order:
                continue
            message = str(row.get("planning_error") or "").strip() or f"MoveRequest #{row.get('id')} status={row.get('status')}"
            reachtruck_errors_by_order_id[int(order.pk)].append(message)

        task_rows = (
            MoveTask.objects.select_related("request")
            .filter(
                request__context_type=MoveRequest.CONTEXT_MANUAL,
                request__context_id__in=order_id_texts,
                request__destination_zone="OTG",
                updated_at__gte=start_at,
                updated_at__lt=end_at,
            )
            .exclude(status__in=[MoveTask.STATUS_DONE, MoveTask.STATUS_CANCELED])
            .filter(Q(status=MoveTask.STATUS_FAILED) | ~Q(error=""))
            .values("id", "request__context_id", "status", "error")
        )
        for row in task_rows:
            order = order_by_id_text.get(str(row.get("request__context_id") or "").strip())
            if not order:
                continue
            message = str(row.get("error") or "").strip() or f"MoveTask #{row.get('id')} status={row.get('status')}"
            reachtruck_errors_by_order_id[int(order.pk)].append(message)

    try:
        from django.apps import apps

        OtgDeliveryRequest = apps.get_model("otg_reachtruck", "OtgDeliveryRequest")
        otg_rows = (
            OtgDeliveryRequest.objects.filter(
                shipping_order_id__in=list(order_by_id),
                updated_at__gte=start_at,
                updated_at__lt=end_at,
            )
            .exclude(status__in=["done", "canceled"])
            .filter(Q(status="blocked") | ~Q(planning_error=""))
            .values("id", "shipping_order_id", "status", "planning_error", "shortage_boxes")
        )
        for row in otg_rows:
            order_id = int(row.get("shipping_order_id") or 0)
            if order_id not in order_by_id:
                continue
            message = str(row.get("planning_error") or "").strip()
            if not message:
                shortage = int(row.get("shortage_boxes") or 0)
                message = f"OTG request #{row.get('id')} status={row.get('status')}"
                if shortage > 0:
                    message += f", не хватает {shortage} короб."
            reachtruck_errors_by_order_id[order_id].append(message)
    except Exception:
        logger.exception("Could not collect OTG reachtruck errors for shipping problem report")

    sections = {
        "active_blockers": [],
        "box_imbalance": [],
        "missing_fact": [],
        "reachtruck_errors": [],
    }

    def row_for(order: ShippingOrder, detail: str, *, severity: str = "warn") -> dict:
        return {
            "number": str(getattr(order, "display_number", "") or order.number or order.pk),
            "client": str(getattr(order.agency, "agn_name", "") or getattr(order.agency, "fio_agn", "") or "—"),
            "status": order.get_status_display(),
            "detail": detail,
            "href": reverse("shipping:detail", args=[order.pk]),
            "severity": severity,
        }

    for order in orders:
        order.display_number = str(order.number or "")
        fact_count = int(fact_counts_by_number.get(str(order.number or "").strip(), 0) or 0)
        expected_boxes = int(getattr(order, "expected_boxes", 0) or 0)
        is_created_today = start_at <= order.created_at < end_at
        is_operational_today = order.planned_ship_date == today or order.slot_date == today
        discrepancy_status = str(getattr(order, "shipping_discrepancy_status", "") or "").strip()
        if discrepancy_status in _SHIPPING_PROBLEM_REPORT_OPEN_DISCREPANCIES:
            sections["active_blockers"].append(
                row_for(order, f"Расхождение в статусе {discrepancy_status}. Требуется решение/добор.", severity="stop")
            )

        if (
            is_operational_today
            and expected_boxes > 0
            and order.status in _SHIPPING_PROBLEM_REPORT_ACTIVE_STATUSES
            and fact_count < expected_boxes
        ):
            sections["missing_fact"].append(
                row_for(
                    order,
                    f"Факт OTG {fact_count} из {expected_boxes}; не хватает {expected_boxes - fact_count} короб.",
                    severity="stop" if order.status == ShippingOrder.STATUS_PACKED else "warn",
                )
            )

        marketplace_name = str(getattr(getattr(order, "marketplace", None), "name", "") or "").casefold()
        if "ozon" in marketplace_name and (is_created_today or is_operational_today):
            ozon_summary = _ozon_api_summary_for_form(order, fullbox_boxes=[])
            ozon_box_count = (
                _positive_int(ozon_summary.get("boxes_total"))
                or len(ozon_summary.get("gm_cargoes") or [])
                or len(ozon_summary.get("gm_barcodes") or [])
            )
            if ozon_box_count and expected_boxes and ozon_box_count != expected_boxes:
                sections["box_imbalance"].append(
                    row_for(
                        order,
                        f"Ozon {ozon_box_count}, Fullbox {expected_boxes}; разница {abs(ozon_box_count - expected_boxes)} короб.",
                        severity="warn",
                    )
                )

        errors = reachtruck_errors_by_order_id.get(int(order.pk)) or []
        if errors:
            unique_errors = []
            seen_errors = set()
            for error in errors:
                clean_error = str(error or "").strip()
                if not clean_error or clean_error in seen_errors:
                    continue
                seen_errors.add(clean_error)
                unique_errors.append(clean_error)
            sections["reachtruck_errors"].append(row_for(order, "; ".join(unique_errors[:3]), severity="stop"))

    section_meta = [
        ("active_blockers", "Активные блокеры"),
        ("box_imbalance", "Ozon/Fullbox не в балансе"),
        ("missing_fact", "Недостающий факт в OTG"),
        ("reachtruck_errors", "HTTP/операционные ошибки ричтрака"),
    ]
    report_sections = []
    totals = {}
    for key, title in section_meta:
        rows = sections[key]
        totals[key] = len(rows)
        report_sections.append(
            {
                "key": key,
                "title": title,
                "count": len(rows),
                "rows": rows[:limit],
                "omitted": max(len(rows) - limit, 0),
            }
        )

    return {
        "show": True,
        "date_label": today.strftime("%d.%m.%Y"),
        "has_rows": any(totals.values()),
        "sections": report_sections,
        "totals": totals,
    }


def _fbs_outbound_issues_for_shipping_list(
    *,
    scope: str,
    role: str | None,
    client_id: str,
    status: str,
) -> list:
    """Return actionable FBS issues for the shared warehouse shipping journal.

    This is a read-only projection.  The FBS issue remains the only workflow
    document, so rendering it here cannot create a second reserve or stock
    movement through ``ShippingOrder``.
    """
    from fbs.models import FbsExternalIssue
    from fbs.services.external_issues import OPEN, ROLES, warehouse_visible_issue_queryset

    if scope != "staff" or role not in ROLES:
        return []

    queryset = warehouse_visible_issue_queryset(
        FbsExternalIssue.objects.select_related("agency", "created_by")
    ).filter(status__in=OPEN)
    if client_id.isdigit():
        queryset = queryset.filter(agency_id=int(client_id))
    if status:
        queryset = queryset.filter(status=status)
    return list(
        queryset.annotate(
            line_count=Count("lines"),
            total_requested_qty=Sum("lines__requested_qty"),
        ).order_by("-created_at", "-id")[:100]
    )


def build_shipping_list_page_context(
    *,
    request,
    scope: str,
    role: str | None,
    client_agency,
) -> dict:
    from . import selectors as shipping_selectors
    from . import views as shipping_views

    # Prod schema may lag local (no parent / destinations / child_orders yet).
    related_names = {f.name for f in ShippingOrder._meta.get_fields()}
    prefetch = ["items"]
    for name in ("child_orders", "destinations"):
        if name in related_names:
            prefetch.append(name)
    qs = ShippingOrder.objects.select_related("agency", "created_by", "marketplace").prefetch_related(
        *prefetch
    )
    if "parent" in related_names:
        qs = qs.filter(parent__isnull=True)
    selected_client = client_agency if scope == "client" else None
    client_id = ""
    if scope == "client" and client_agency is not None:
        qs = qs.filter(agency=client_agency)
    else:
        client_id = str(request.GET.get("client") or "").strip()
        if client_id.isdigit():
            qs = qs.filter(agency_id=int(client_id))
            selected_client = Agency.objects.filter(id=int(client_id), archived=False).first()
    status = str(request.GET.get("status") or "").strip()
    if status:
        qs = qs.filter(status=status)
    fbs_outbound_issues = _fbs_outbound_issues_for_shipping_list(
        scope=scope,
        role=role,
        client_id=client_id,
        status=status,
    )
    from .ozon_template import ozon_meta_from_comment

    orders = list(qs.order_by("-created_at", "-id")[:300])
    for order in orders:
        # The list page can contain many orders. Resolving the warehouse state
        # for each row is too expensive on production and can make gunicorn kill
        # the request before the page is rendered. Keep the detailed warehouse
        # status for detail/workflow pages; the journal uses the persisted
        # document status.
        order.ui_status_label = order.get_status_display()
        order.display_number = shipping_views._display_shipping_number(order.number)
        destinations = []
        children = []
        order.ozon_destination_count = 0
        order.ozon_child_count = 0
        order.is_ozon_parent = False
        order.ozon_distribution_label = ""
        try:
            from .distribution import DISTRIBUTION_LABELS

            destinations = list(order.destinations.all())
            children = list(order.child_orders.all())  # legacy parent/child packets
            order.ozon_destination_count = len(destinations)
            order.ozon_child_count = len(destinations) or len(children)
            order.is_ozon_parent = bool(destinations) or bool(children)
            order.ozon_distribution_label = DISTRIBUTION_LABELS.get(
                str(getattr(order, "distribution_status", "") or ""), ""
            )
        except Exception:
            destinations = []
            children = []
        if destinations:
            order.ozon_warehouse_summary = ", ".join(
                str(d.warehouse_name or "-") for d in destinations[:4]
            )
            if len(destinations) > 4:
                order.ozon_warehouse_summary += f" +{len(destinations) - 4}"
            ozon_meta = ozon_meta_from_comment(order.comment)
            order.ozon_batch_id = ozon_meta.get("batch_id") or ""
            order.is_ozon_batch = True
            order.ozon_transit = str(order.transit_address or "") or "—"
        elif children:
            order.ozon_warehouse_summary = ", ".join(
                str(child.destination_warehouse or "-") for child in children[:4]
            )
            if len(children) > 4:
                order.ozon_warehouse_summary += f" +{len(children) - 4}"
            ozon_meta = ozon_meta_from_comment(order.comment)
            order.ozon_batch_id = ozon_meta.get("batch_id") or ""
            order.is_ozon_batch = True
            order.ozon_transit = str(order.transit_address or "") or "—"
        else:
            order.ozon_warehouse_summary = str(order.destination_warehouse or "-")
            ozon_meta = ozon_meta_from_comment(order.comment)
            order.ozon_batch_id = ozon_meta.get("batch_id") or ""
            order.is_ozon_batch = bool(ozon_meta.get("is_ozon_batch"))
            order.ozon_transit = "—"
    return {
        "orders": orders,
        "fbs_outbound_issues": fbs_outbound_issues,
        "shipping_problem_report": _shipping_problem_report_context(
            scope=scope,
            role=role,
            selected_client=selected_client,
        ),
        "status": status,
        "client_filter": client_id,
        "scope": scope,
        "role": role,
        "can_write": shipping_views._can_write(scope, role),
        "selected_client": selected_client,
        "cabinet_url": resolve_cabinet_url(
            role,
            client_id=selected_client.id if scope == "client" and selected_client else None,
        ),
        "status_choices": ShippingOrder.STATUS_CHOICES,
        "clients": _staff_client_queryset_for_role(role) if scope == "staff" else [],
    }


def build_shipping_detail_page_context(
    *,
    request,
    order: ShippingOrder,
    scope: str,
    role: str | None,
) -> dict:
    from . import selectors as shipping_selectors
    from . import transport_note as shipping_transport_note
    from . import views as shipping_views

    can_write = shipping_views._can_write(scope, role)
    can_edit_items = shipping_views._can_edit_items(scope, role, order)
    can_submit_for_approval = shipping_views._can_submit_for_approval(scope, role, order)
    can_manager_approve = shipping_views._can_manager_approve(scope, role, order)
    can_manager_reopen = shipping_views._can_manager_reopen(scope, role, order)
    can_storekeeper_accept = shipping_views._can_storekeeper_accept(scope, role, order)
    can_storekeeper_pick = shipping_views._can_storekeeper_pick(scope, role, order)
    pick_readiness = shipping_pick_readiness(order) if can_storekeeper_pick else {"can_pick": False, "reason": ""}
    can_storekeeper_pack = shipping_views._can_storekeeper_pack(scope, role, order)
    can_storekeeper_manage_packing = shipping_views._can_storekeeper_manage_packing(scope, role, order)
    can_cancel = shipping_views._can_cancel(scope, role, order)
    can_edit_order_form = shipping_views._can_edit_order_form(scope, role, order)
    stock_rows_for_order: list[dict] = []
    available_items_for_order: list[dict] = []
    if can_edit_items:
        stock_rows_for_order = shipping_views._shipping_stock_picker_rows(order.agency, exclude_order=order)
        available_items_for_order = shipping_available_items(order, exclude_order=order)

    order.display_number = shipping_views._display_shipping_number(order.number)
    context = shipping_selectors.build_shipping_detail_context(
        order,
        scope=scope,
        role=role,
        stock_rows=stock_rows_for_order,
        available_items=available_items_for_order,
        order_box_count=shipping_views._order_box_count(order),
        can_write=can_write,
        can_edit_items=can_edit_items,
        can_submit_for_approval=can_submit_for_approval,
        can_manager_approve=can_manager_approve,
        can_manager_reopen=can_manager_reopen,
        can_storekeeper_accept=can_storekeeper_accept,
        can_storekeeper_pick=can_storekeeper_pick,
        can_storekeeper_pack=can_storekeeper_pack,
        can_storekeeper_manage_packing=can_storekeeper_manage_packing,
        can_cancel=can_cancel,
        can_edit_order_form=can_edit_order_form,
    )
    context["shipping_client_comment"] = _strip_ozon_api_comment_block(order.comment)
    context["pick_readiness"] = pick_readiness
    context["can_storekeeper_pick"] = can_storekeeper_pick and bool(pick_readiness["can_pick"])
    context["storekeeper_pick_unavailable_reason"] = (
        str(pick_readiness["reason"] or "").strip()
        if can_storekeeper_pick and not pick_readiness["can_pick"]
        else ""
    )
    context["can_create_pickup_trip"] = (
        scope == "staff"
        and role in {"storekeeper", "manager", "head_manager", "director", "admin", "developer"}
        and order.delivery_type == ShippingOrder.DELIVERY_PICKUP
        and order.status == ShippingOrder.STATUS_PACKED
        and not bool((context.get("dispatch_stage") or {}).get("has_trip"))
    )
    context["can_complete_transfer"] = (
        scope == "staff"
        and shipping_views._is_storekeeper_role(role)
        and order.delivery_type == ShippingOrder.DELIVERY_TRANSFER
        and order.status == ShippingOrder.STATUS_PACKED
        and not bool((context.get("dispatch_stage") or {}).get("has_trip"))
    )
    context["can_manage_transport_note"] = shipping_transport_note.can_access_transport_note(order, scope, role)
    context["transport_note_url"] = reverse("shipping:documents", args=[order.pk])
    # Жёлтая зона: только чтение связи из receiving_distribution (без смены статусов/резервов).
    context["distribution_receiving_order_id"] = ""
    try:
        from receiving_distribution.models import ReceivingDistributionDirection

        link = (
            ReceivingDistributionDirection.objects.filter(shipping_order_id=order.pk)
            .select_related("plan")
            .first()
        )
        if link and link.plan_id:
            context["distribution_receiving_order_id"] = str(link.plan.receiving_order_id or "")
    except Exception:
        context["distribution_receiving_order_id"] = ""
    loose_state = shipping_views._shipping_loose_packing_initial_state(order)
    loose_packing_items = (
        list(loose_state.get("loose_items") or [])
        if isinstance(loose_state, dict)
        else []
    )
    context["has_loose_packing_items"] = bool(loose_packing_items)
    context["ozon_gm_repack"] = {"required": False, "ready": False, "errors": []}
    context["loose_packing_items"] = loose_packing_items
    context["loose_packing_qty"] = sum(
        max(int(item.get("qty") or 0), 0)
        for item in loose_packing_items
        if isinstance(item, dict)
    )
    from .ozon_template import ozon_meta_from_comment

    context["ozon_meta"] = ozon_meta_from_comment(order.comment)
    context["ozon_child_orders"] = []
    context["ozon_destinations"] = []
    context["ozon_has_distribution"] = False
    context["ozon_is_parent"] = False
    context["ozon_parent_order"] = None
    context["ozon_distribution_status"] = ""
    context["ozon_distribution_label"] = "—"
    context["ozon_transit"] = str(getattr(order, "transit_address", "") or "") or "—"
    context["ozon_supply_number"] = str(getattr(order, "supply_number", "") or "") or "—"
    context["ozon_detail_is_ozon"] = str(getattr(getattr(order, "marketplace", None), "name", "") or "").strip().lower() == "ozon"
    context["ozon_detail_supply_number"] = (
        str(getattr(order, "supply_number", "") or "").strip()
        or _value_from_ozon_api_comment(order.comment, "Номер поставки Ozon")
        or str(getattr(order, "wb_supply_barcode", "") or "").strip()
        or "—"
    )
    delivered_boxes = shipping_views._shipping_delivered_boxes(order)
    ozon_match_boxes = list(delivered_boxes)
    if not ozon_match_boxes:
        ozon_match_boxes = _ozon_reserved_fullbox_boxes(order)
    ozon_detail_summary = _ozon_api_summary_for_form(
        order,
        fullbox_boxes=ozon_match_boxes,
    )
    context["ozon_detail_summary"] = ozon_detail_summary
    context["ozon_gm_repack"] = _ozon_gm_repack_context(
        order,
        delivered_boxes=delivered_boxes,
    )
    if context["ozon_gm_repack"].get("required") and not context["ozon_gm_repack"].get("ready"):
        warnings = list(context.get("shipping_integrity_warnings") or [])
        warnings.extend(context["ozon_gm_repack"].get("errors") or [])
        context["shipping_integrity_warnings"] = list(dict.fromkeys(warnings))
        context["can_storekeeper_manage_packing"] = False
    context["storekeeper_binding_alert"] = _storekeeper_marketplace_binding_alert(
        order,
        boxes=delivered_boxes,
    )
    context["ozon_detail_supply_number"] = (
        str(ozon_detail_summary.get("supply_number") or "").strip()
        or context["ozon_detail_supply_number"]
    )
    context["ozon_detail_shipping_barcode"] = (
        str(ozon_detail_summary.get("shipping_barcode") or "").strip()
        or str(getattr(order, "shipping_barcode", "") or "").strip()
        or "—"
    )
    context["ozon_detail_destination_warehouse"] = (
        str(ozon_detail_summary.get("destination_warehouse") or "").strip()
        or str(getattr(order, "destination_warehouse", "") or "").strip()
        or "—"
    )
    context["ozon_detail_gm_barcodes"] = (
        str(ozon_detail_summary.get("gm_barcodes_label") or "").strip()
        or _value_from_ozon_api_comment(order.comment, "ШК ГМ Ozon")
    )
    context["ozon_detail_boxes_total"] = (
        str(ozon_detail_summary.get("boxes_total") or "").strip()
        or _value_from_ozon_api_comment(order.comment, "Коробов Ozon")
    )
    context["ozon_directions"] = []
    context["ozon_destination_rows"] = []
    context["ozon_distribution_matrix"] = None
    # Ozon destination distribution requires models that may be absent.
    # Never block storekeeper/manager shipping detail when that layer is unavailable.
    try:
        from .distribution import DISTRIBUTION_LABELS, destination_summary_rows, distribution_matrix

        destinations = list(
            order.destinations.prefetch_related("items__order_item").order_by("sort_order", "id")
        )
        children = list(
            order.child_orders.select_related("marketplace")
            .prefetch_related("items")
            .order_by("destination_warehouse", "id")
        )
        context["ozon_child_orders"] = children
        context["ozon_destinations"] = destinations
        context["ozon_has_distribution"] = bool(destinations)
        context["ozon_is_parent"] = bool(destinations) or bool(children)
        context["ozon_parent_order"] = order.parent if getattr(order, "parent_id", None) else None
        context["ozon_distribution_status"] = getattr(order, "distribution_status", "") or ""
        context["ozon_distribution_label"] = DISTRIBUTION_LABELS.get(
            str(getattr(order, "distribution_status", "") or ""), "—"
        )
        if destinations:
            context["ozon_destination_rows"] = destination_summary_rows(order)
            context["ozon_distribution_matrix"] = distribution_matrix(order)
            context["ozon_directions"] = [
                {
                    "order": None,
                    "destination": row["destination"],
                    "warehouse": row["warehouse"],
                    "boxes": row["boxes"],
                    "qty": row["units"],
                    "sku_count": row["sku_count"],
                    "gm_lines": (
                        [row["destination"].comment]
                        if row["destination"].comment
                        else []
                    ),
                    "status_label": context["ozon_distribution_label"],
                    "fill_pct": row["fill_pct"],
                }
                for row in context["ozon_destination_rows"]
            ]
        else:
            child_rows = []
            for child in children:
                child.display_number = shipping_views._display_shipping_number(child.number)
                child.ui_status_label = shipping_selectors.shipping_ui_status_label(child)
                meta = ozon_meta_from_comment(child.comment)
                child_rows.append(
                    {
                        "order": child,
                        "destination": None,
                        "warehouse": child.destination_warehouse or "-",
                        "boxes": int(child.expected_boxes or 0),
                        "qty": sum(int(item.qty_requested or 0) for item in child.items.all()),
                        "gm_lines": meta.get("gm_lines") or [],
                        "status_label": child.ui_status_label,
                        "sku_count": child.items.count(),
                        "fill_pct": 100,
                    }
                )
            context["ozon_directions"] = child_rows
    except Exception:
        pass

    # Кнопки согласования расхождения для менеджера (шаблон ждёт эти флаги в context).
    try:
        from .discrepancy import (
            can_add_discrepancy_item,
            can_approve_shipping_discrepancy,
            can_confirm_shipping_discrepancy_pick,
            can_request_shipping_discrepancy,
            shipping_discrepancy_context,
        )

        integrity_warnings = list(context.get("shipping_integrity_warnings") or [])
        can_approve_disc = can_approve_shipping_discrepancy(scope, role, order)
        shipping_discrepancy = shipping_discrepancy_context(
            order, warnings=integrity_warnings
        )
        integrity_warnings = _visible_shipping_integrity_warnings(
            integrity_warnings,
            shipping_discrepancy,
        )
        context["shipping_integrity_warnings"] = integrity_warnings
        context["shipping_discrepancy"] = shipping_discrepancy
        context["ozon_gm_discrepancy_panel"] = _build_ozon_gm_discrepancy_panel(
            order,
            ozon_summary=ozon_detail_summary,
            delivered_boxes=delivered_boxes,
            discrepancy_context=shipping_discrepancy,
        )
        context["can_request_shipping_discrepancy"] = can_request_shipping_discrepancy(
            scope, role, order, integrity_warnings
        )
        context["can_approve_shipping_discrepancy"] = can_approve_disc
        context["can_add_discrepancy_item"] = can_add_discrepancy_item(
            scope,
            role,
            order,
        )
        context["can_confirm_shipping_discrepancy_pick"] = (
            can_confirm_shipping_discrepancy_pick(scope, role, order)
        )
        if context["can_add_discrepancy_item"] and not context.get("stock_rows"):
            context["stock_rows"] = shipping_views._shipping_stock_picker_rows(
                order.agency, exclude_order=order
            )
    except Exception:
        context.setdefault("shipping_discrepancy", {"has_history": False})
        context.setdefault("ozon_gm_discrepancy_panel", {"show": False})
        context.setdefault("can_request_shipping_discrepancy", False)
        context.setdefault("can_approve_shipping_discrepancy", False)
        context.setdefault("can_add_discrepancy_item", False)
        context.setdefault("can_confirm_shipping_discrepancy_pick", False)
    ozon_intake_errors = _strict_ozon_gm_intake_errors(order)
    if ozon_intake_errors:
        warnings = list(context.get("shipping_integrity_warnings") or [])
        warnings.append(
            "Паллетизация заблокирована: сохранённые данные Ozon не позволяют "
            "однозначно сопоставить грузовые места с заявленным товаром."
        )
        warnings.extend(ozon_intake_errors)
        context["shipping_integrity_warnings"] = list(dict.fromkeys(warnings))
        context["can_storekeeper_pack"] = False
        context["can_storekeeper_manage_packing"] = False
    return context


def _shipping_use_client_lk_form(*, scope: str, request, selected_client) -> bool:
    """Client portal, or staff opened shipping from client LK via ?client=."""
    if scope == "client":
        return True
    if scope != "staff" or selected_client is None:
        return False
    client_id = str(request.GET.get("client") or request.POST.get("client") or "").strip()
    return client_id.isdigit() and int(client_id) == int(selected_client.id)


def _submission_expected_box_count(
    *,
    selected_box_count: int,
    is_ozon: bool,
    ozon_api_meta: dict | None,
) -> int:
    """Use Ozon cargo places as the final box count, not source warehouse boxes."""
    selected_count = max(int(selected_box_count or 0), 0)
    if not is_ozon or not isinstance(ozon_api_meta, dict):
        return selected_count
    try:
        ozon_count = max(int(ozon_api_meta.get("boxes_total") or 0), 0)
    except (TypeError, ValueError):
        ozon_count = 0
    return ozon_count or selected_count


def _inject_stock_box_selections(
    request,
    stock_rows: list[dict],
    selected_boxes: dict[str, str],
    *,
    partial_box_splits: list[dict] | None = None,
) -> None:
    post = request.POST.copy()
    keys: list[str] = []
    boxes: list[str] = []
    for row in stock_rows or []:
        key = str(row.get("key") or "").strip()
        keys.append(key)
        boxes.append(str(selected_boxes.get(key, "0") or "0"))
    post.setlist("stock_key_all[]", keys)
    post.setlist("stock_boxes[]", boxes)
    if partial_box_splits is not None:
        post["stock_partial_box_splits_json"] = json.dumps(
            list(partial_box_splits or []),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    request.POST = post


def _client_quantity_identity(row: dict) -> tuple[str, str, str, str]:
    """Identity used by the quantity-only client picker.

    The warehouse reserve already works by these four fields. Product names and
    physical source boxes are deliberately excluded from the submitted key.
    """
    return (
        str(row.get("sku_code") or row.get("sku") or "").strip().casefold(),
        str(row.get("size") or "").strip().casefold(),
        str(row.get("barcode") or "").strip().casefold(),
        str(row.get("goods_type") or "").strip().casefold(),
    )


def _client_quantity_key(identity: tuple[str, str, str, str]) -> str:
    payload = "\x1f".join(identity).encode("utf-8")
    return "qty:" + hashlib.sha256(payload).hexdigest()[:24]


def _client_quantity_values_from_request(request) -> tuple[dict[str, str], list[str]]:
    keys = request.POST.getlist("stock_quantity_key[]") or request.POST.getlist("stock_quantity_key")
    quantities = request.POST.getlist("stock_quantity[]") or request.POST.getlist("stock_quantity")
    values: dict[str, str] = {}
    errors: list[str] = []
    for index, raw_key in enumerate(keys):
        key = str(raw_key or "").strip()
        if not key:
            continue
        if key in values:
            errors.append("В форме повторяется одна и та же товарная позиция. Обновите страницу.")
            continue
        values[key] = str(quantities[index] if index < len(quantities) else "").strip()
    return values, errors


def _client_quantity_whole_box_count(row: dict) -> int:
    """Count complete physical mono-boxes, including boxes holding one unit."""
    if row.get("partial_only") or row.get("is_mixed_box"):
        return 0
    box_qty = max(int(row.get("box_qty") or 0), 0)
    if not box_qty:
        return 0
    # These codes come from the live warehouse picker, not client POST data.
    # A loose unit or a synthetic quantity must not become a physical box.
    codes = {str(code or "").strip().casefold() for code in row.get("box_codes") or []}
    codes.discard("")
    return min(
        max(int(row.get("available_boxes") or 0), 0),
        max(int(row.get("available_qty") or 0), 0) // box_qty,
        len(codes),
    )


def _client_quantity_stock_groups(stock_rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str, str], dict] = {}
    for source_index, raw_row in enumerate(stock_rows or []):
        row = dict(raw_row or {})
        row_key = str(row.get("key") or "").strip()
        available_qty = max(int(row.get("available_qty") or 0), 0)
        if not row_key or available_qty <= 0:
            continue
        identity = _client_quantity_identity(row)
        if not identity[0]:
            continue
        entry = grouped.setdefault(
            identity,
            {
                "key": _client_quantity_key(identity),
                "identity": identity,
                "sku_code": str(row.get("sku_code") or "").strip(),
                "name": str(row.get("name") or "").strip(),
                "size": str(row.get("size") or "").strip(),
                "barcode": str(row.get("barcode") or "").strip(),
                "goods_type": str(row.get("goods_type") or "").strip(),
                "available_qty": 0,
                "source_rows": [],
                "pack_counts": defaultdict(int),
            },
        )
        row["_client_source_index"] = source_index
        entry["source_rows"].append(row)
        entry["available_qty"] += available_qty
        available_boxes = _client_quantity_whole_box_count(row)
        if available_boxes > 0:
            entry["pack_counts"][int(row["box_qty"])] += available_boxes

    result: list[dict] = []
    for entry in grouped.values():
        pack_options = [
            {"multiplicity": multiplicity, "box_count": box_count}
            for multiplicity, box_count in sorted(
                entry.pop("pack_counts").items(),
                reverse=True,
            )
        ]
        entry["pack_options"] = pack_options
        entry["pack_options_json"] = json.dumps(
            pack_options,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result.append(entry)
    return sorted(
        result,
        key=lambda row: (
            str(row.get("sku_code") or "").casefold(),
            str(row.get("size") or "").casefold(),
            str(row.get("barcode") or "").casefold(),
            str(row.get("goods_type") or "").casefold(),
        ),
    )


def _client_quantity_plan(group: dict, target_qty: int):
    availability: list[tuple[int, int]] = []
    piece_capacity = 0
    for row in group.get("source_rows") or []:
        box_count = _client_quantity_whole_box_count(row)
        box_qty = max(int(row.get("box_qty") or 0), 0)
        if box_count:
            availability.append((box_qty, box_count))
        # Keep partial/mixed capacity separate: encoding it as (1, qty)
        # would turn loose pieces into whole one-unit boxes.
        piece_capacity += max(int(row.get("available_qty") or 0) - box_qty * box_count, 0)
    plan = pick_boxes_for_quantity(
        target_qty,
        availability,
        unit_containers_are_pieces=False,
    )
    missing_qty = max(plan.missing_qty - piece_capacity, 0)
    return BoxPickPlan(
        lines=plan.lines,
        piece_qty=plan.piece_qty,
        missing_qty=missing_qty,
        unattainable=bool(missing_qty),
        tie_break_fallback_used=plan.tie_break_fallback_used,
    )


def _client_quantity_rows_for_context(
    *,
    stock_rows: list[dict],
    request,
    edit_order=None,
) -> list[dict]:
    groups = _client_quantity_stock_groups(stock_rows)
    selected: dict[str, str] = {}
    if getattr(request, "method", "") == "POST" and str(
        request.POST.get("stock_input_mode") or ""
    ).strip() == "quantity":
        selected, _errors = _client_quantity_values_from_request(request)
    elif edit_order is not None:
        totals: defaultdict[tuple[str, str, str, str], int] = defaultdict(int)
        for item in edit_order.items.all():
            identity = _client_quantity_identity(
                {
                    "sku_code": getattr(item, "sku_code", ""),
                    "size": getattr(item, "size", ""),
                    "barcode": getattr(item, "barcode", ""),
                    "goods_type": getattr(item, "goods_type", ""),
                }
            )
            totals[identity] += max(int(getattr(item, "qty_requested", 0) or 0), 0)
        selected = {
            _client_quantity_key(identity): str(quantity)
            for identity, quantity in totals.items()
            if quantity > 0
        }

    for group in groups:
        raw_value = str(selected.get(str(group.get("key") or ""), "") or "").strip()
        group["selected_qty"] = raw_value
        try:
            target_qty = max(int(raw_value or 0), 0)
        except (TypeError, ValueError):
            target_qty = 0
        plan = _client_quantity_plan(group, target_qty) if target_qty > 0 else None
        group["planned_whole_boxes"] = sum(
            line.box_count for line in (plan.lines if plan else ())
        )
        group["planned_piece_qty"] = int(plan.piece_qty if plan else 0)
    return groups


def _client_quantity_mode_requested(request) -> bool:
    if getattr(request, "method", "") != "POST":
        return False
    if str(request.POST.get("stock_input_mode") or "").strip() != "quantity":
        return False
    # The Ozon API and Excel flows already submit a verified legacy selection.
    # Leaving those untouched preserves their tariff and GM contracts.
    if str(request.POST.get("ozon_supply_payload_json") or "").strip():
        return False
    return str(request.POST.get("action") or "").strip() not in {
        "import_template",
        "import_ozon_template",
    }


def _client_quantity_split(
    *,
    row: dict,
    quantity: int,
    suffix: str,
    items: list[dict] | None = None,
) -> dict:
    row_key = str(row.get("key") or "").strip()
    return {
        "identity": f"client-quantity:{suffix}",
        "group_key": f"client-quantity:{suffix}",
        "row_key": row_key,
        "group": str(row.get("mixed_group") or "").strip(),
        "boxes": 1,
        "items": list(items or [{"key": row_key, "qty": int(quantity)}]),
        "piece_qty": int(quantity),
        "source_box_qty": max(int(row.get("box_qty") or 0), 0),
    }


def _materialize_client_quantity_selection(request, stock_rows: list[dict]) -> list[str]:
    """Translate client quantities into the unchanged legacy box/split POST.

    This function only prepares the request consumed by the current parser.
    It does not create orders, reserve stock or modify warehouse state.
    """
    values, errors = _client_quantity_values_from_request(request)
    groups = _client_quantity_stock_groups(stock_rows)
    groups_by_key = {str(group.get("key") or ""): group for group in groups}
    selected_boxes: defaultdict[str, int] = defaultdict(int)
    remaining_boxes: dict[str, int] = {
        str(row.get("key") or "").strip(): max(int(row.get("available_boxes") or 0), 0)
        for row in stock_rows or []
        if str(row.get("key") or "").strip()
    }
    piece_remaining: dict[str, int] = {}
    splits: list[dict] = []

    for quantity_key, raw_quantity in values.items():
        group = groups_by_key.get(quantity_key)
        if group is None:
            errors.append("Выбрана неактуальная товарная позиция. Обновите страницу и повторите.")
            continue
        if raw_quantity == "":
            continue
        try:
            target_qty = int(raw_quantity)
        except (TypeError, ValueError):
            errors.append(f"{group['sku_code']}: количество должно быть целым числом.")
            continue
        if target_qty < 0:
            errors.append(f"{group['sku_code']}: количество не может быть отрицательным.")
            continue
        if target_qty == 0:
            continue
        available_qty = max(int(group.get("available_qty") or 0), 0)
        if target_qty > available_qty:
            errors.append(
                f"{group['sku_code']}/{group.get('size') or '-'}: "
                f"запрошено {target_qty} шт., доступно {available_qty} шт."
            )
            continue

        plan = _client_quantity_plan(group, target_qty)
        if plan.unattainable or plan.missing_qty:
            errors.append(
                f"{group['sku_code']}/{group.get('size') or '-'}: "
                f"не удалось набрать {target_qty} шт. из текущего свободного остатка."
            )
            continue

        for line in plan.lines:
            boxes_needed = int(line.box_count)
            candidates = sorted(
                [
                    row
                    for row in group.get("source_rows") or []
                    if not row.get("partial_only")
                    and not row.get("is_mixed_box")
                    and int(row.get("box_qty") or 0) == int(line.multiplicity)
                ],
                key=lambda row: int(row.get("_client_source_index") or 0),
            )
            for row in candidates:
                if boxes_needed <= 0:
                    break
                row_key = str(row.get("key") or "").strip()
                take = min(max(int(remaining_boxes.get(row_key, 0)), 0), boxes_needed)
                if take <= 0:
                    continue
                selected_boxes[row_key] += take
                remaining_boxes[row_key] -= take
                boxes_needed -= take
            if boxes_needed:
                errors.append(
                    f"{group['sku_code']}: остатки изменились во время расчёта. Обновите страницу."
                )
        piece_remaining[quantity_key] = max(int(plan.piece_qty), 0)

    # First consume non-mixed piece sources. Open remainders are preferred,
    # then unit containers, then one untouched full box.
    for quantity_key, quantity in list(piece_remaining.items()):
        if quantity <= 0:
            continue
        group = groups_by_key[quantity_key]
        source_rows = list(group.get("source_rows") or [])

        partial_rows = sorted(
            [row for row in source_rows if row.get("partial_only") and not row.get("is_mixed_box")],
            key=lambda row: int(row.get("_client_source_index") or 0),
        )
        unit_rows = sorted(
            [
                row
                for row in source_rows
                if not row.get("partial_only")
                and not row.get("is_mixed_box")
                and int(row.get("box_qty") or 0) == 1
            ],
            key=lambda row: int(row.get("_client_source_index") or 0),
        )
        full_rows = sorted(
            [
                row
                for row in source_rows
                if not row.get("partial_only")
                and not row.get("is_mixed_box")
                and int(row.get("box_qty") or 0) > 1
            ],
            key=lambda row: (
                0 if row.get("is_open_remainder") else 1,
                int(row.get("box_qty") or 0),
                int(row.get("_client_source_index") or 0),
            ),
        )

        for row in partial_rows:
            if quantity <= 0:
                break
            row_key = str(row.get("key") or "").strip()
            available_boxes = max(int(remaining_boxes.get(row_key, 0)), 0)
            capacity = max(int(row.get("split_available_qty") or 0), 0)
            for box_index in range(available_boxes):
                if quantity <= 0:
                    break
                take = min(quantity, capacity)
                if take <= 0:
                    continue
                splits.append(
                    _client_quantity_split(
                        row=row,
                        quantity=take,
                        suffix=f"{quantity_key}:partial:{box_index}",
                    )
                )
                remaining_boxes[row_key] -= 1
                quantity -= take

        for row in unit_rows:
            if quantity <= 0:
                break
            row_key = str(row.get("key") or "").strip()
            take = min(max(int(remaining_boxes.get(row_key, 0)), 0), quantity)
            if take <= 0:
                continue
            selected_boxes[row_key] += take
            remaining_boxes[row_key] -= take
            quantity -= take

        for row in full_rows:
            if quantity <= 0:
                break
            row_key = str(row.get("key") or "").strip()
            available_boxes = max(int(remaining_boxes.get(row_key, 0)), 0)
            capacity = max(int(row.get("box_qty") or 0) - 1, 0)
            for box_index in range(available_boxes):
                if quantity <= 0:
                    break
                take = min(quantity, capacity)
                if take <= 0:
                    continue
                splits.append(
                    _client_quantity_split(
                        row=row,
                        quantity=take,
                        suffix=f"{quantity_key}:full:{box_index}",
                    )
                )
                remaining_boxes[row_key] -= 1
                quantity -= take
        piece_remaining[quantity_key] = quantity

    # Mixed boxes are shared physical sources. Allocate all requested product
    # identities into the same split record for each box, or promote a fully
    # consumed composition to the existing whole-mixed-box representation.
    mixed_groups: dict[str, list[dict]] = defaultdict(list)
    for group in groups:
        for row in group.get("source_rows") or []:
            mixed_group = str(row.get("mixed_group") or "").strip()
            if row.get("is_mixed_box") and mixed_group:
                row["_client_quantity_key"] = str(group.get("key") or "")
                mixed_groups[mixed_group].append(row)

    for mixed_group, group_rows in sorted(mixed_groups.items()):
        group_rows = sorted(
            group_rows,
            key=lambda row: (
                str(row.get("_client_quantity_key") or ""),
                int(row.get("_client_source_index") or 0),
            ),
        )
        available_boxes = min(
            max(int(remaining_boxes.get(str(row.get("key") or "").strip(), 0)), 0)
            for row in group_rows
        )
        for box_index in range(available_boxes):
            items: list[dict] = []
            for row in group_rows:
                quantity_key = str(row.get("_client_quantity_key") or "")
                needed = max(int(piece_remaining.get(quantity_key, 0)), 0)
                if needed <= 0:
                    continue
                capacity = max(
                    int(
                        (
                            row.get("split_available_qty")
                            if row.get("partial_only")
                            else row.get("box_qty")
                        )
                        or 0
                    ),
                    0,
                )
                take = min(needed, capacity)
                if take <= 0:
                    continue
                items.append({"key": str(row.get("key") or "").strip(), "qty": take})
                piece_remaining[quantity_key] = needed - take
            if not items:
                continue

            item_qty = {str(item["key"]): int(item["qty"]) for item in items}
            is_complete_whole_box = bool(
                not any(row.get("partial_only") for row in group_rows)
                and len(item_qty) == len(group_rows)
                and all(
                    item_qty.get(str(row.get("key") or "").strip(), 0)
                    == max(int(row.get("box_qty") or 0), 0)
                    for row in group_rows
                )
            )
            if is_complete_whole_box:
                for row in group_rows:
                    row_key = str(row.get("key") or "").strip()
                    selected_boxes[row_key] += 1
                    remaining_boxes[row_key] -= 1
                continue

            anchor = group_rows[0]
            splits.append(
                _client_quantity_split(
                    row=anchor,
                    quantity=sum(item_qty.values()),
                    suffix=f"mixed:{hashlib.sha256(mixed_group.encode('utf-8')).hexdigest()[:12]}:{box_index}",
                    items=items,
                )
            )
            for row in group_rows:
                row_key = str(row.get("key") or "").strip()
                remaining_boxes[row_key] -= 1

    for quantity_key, quantity in piece_remaining.items():
        if quantity <= 0:
            continue
        group = groups_by_key[quantity_key]
        errors.append(
            f"{group['sku_code']}/{group.get('size') or '-'}: "
            f"не удалось определить источник для поштучного отбора {quantity} шт. "
            "Обновите страницу и повторите."
        )

    _inject_stock_box_selections(
        request,
        stock_rows,
        {key: str(value) for key, value in selected_boxes.items() if value > 0},
        partial_box_splits=splits,
    )
    return errors


def _copy_shipping_proto_fields(proto: ShippingOrder, *, skip: set[str]) -> ShippingOrder:
    order = ShippingOrder()
    for field in ShippingOrder._meta.concrete_fields:
        name = field.name
        if name in skip:
            continue
        setattr(order, name, getattr(proto, name))
    return order


def _apply_planned_ship_date(
    order: ShippingOrder,
    *,
    submitted_at=None,
    submitted: bool = True,
) -> None:
    if order.eta_at is not None:
        planned_eta = order.eta_at
        if timezone.is_naive(planned_eta):
            planned_eta = timezone.make_aware(planned_eta, timezone.get_current_timezone())
            order.eta_at = planned_eta
        order.planned_ship_date = timezone.localtime(planned_eta).date()
        return
    if submitted and order.delivery_type in _SUBMISSION_DATE_DELIVERY_TYPES:
        order.planned_ship_date = _local_date(submitted_at)
        return
    order.planned_ship_date = None


def _sync_ozon_eta_after_slot_change(
    order: ShippingOrder,
    previous_order: ShippingOrder | None,
) -> None:
    """Keep Ozon dates coupled when only the slot date was edited.

    Ozon initially fills both dates from one marketplace slot.  If they were
    still equal before editing and only ``slot_date`` changed, leaving the
    hidden ``eta_at`` untouched would silently restore the old date after the
    form is submitted.
    """
    if previous_order is None or not _is_current_ozon_shipping(order):
        return
    previous_slot = previous_order.slot_date
    current_slot = order.slot_date
    if not previous_slot or not current_slot or previous_slot == current_slot:
        return
    if previous_order.eta_at is None or order.eta_at is None:
        return

    def local_eta(value):
        if timezone.is_naive(value):
            value = timezone.make_aware(value, timezone.get_current_timezone())
        return timezone.localtime(value)

    previous_eta = local_eta(previous_order.eta_at)
    current_eta = local_eta(order.eta_at)
    if previous_eta.date() != previous_slot or current_eta.date() != previous_eta.date():
        return
    shifted = datetime.combine(current_slot, current_eta.timetz().replace(tzinfo=None))
    order.eta_at = timezone.make_aware(shifted, timezone.get_current_timezone())


def _apply_ozon_eta_fields(order: ShippingOrder) -> None:
    _apply_planned_ship_date(order)
    order.wb_supply_barcode = ""


def _save_staff_form_documents(*, request, order: ShippingOrder) -> list[str]:
    """Save explicit staff-form uploads without deleting or duplicating attachments."""
    from . import web_ui
    from .models import ShippingOrderAttachment

    files = [f for f in request.FILES.getlist("documents") if getattr(f, "name", "")]
    if not files:
        return []
    allowed = {".pdf", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".doc", ".docx", ".txt"}
    existing = list(web_ui._active_shipping_attachments(order))
    identities = set()
    for attachment in existing:
        identities.add((attachment.filename.lower(), attachment.file.size,
                        web_ui._stored_attachment_sha256(attachment)))
    prepared = []
    for uploaded in files:
        name = str(uploaded.name).strip()
        extension = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if extension not in allowed:
            raise ValidationError(f"{name}: допустимы PDF, Excel, Word, TXT, JPG и PNG.")
        if uploaded.size > 25 * 1024 * 1024:
            raise ValidationError(f"{name}: файл больше 25 МБ.")
        identity = (name.lower(), uploaded.size, web_ui._uploaded_file_sha256(uploaded))
        if identity not in identities:
            identities.add(identity)
            prepared.append(uploaded)
    if len(existing) + len(prepared) > 10:
        raise ValidationError("К одной заявке можно прикрепить не более 10 файлов.")
    names = []
    for uploaded in prepared:
        attachment = ShippingOrderAttachment.objects.create(
            order=order, uploaded_by=request.user, file=uploaded,
        )
        names.append(attachment.filename)
    return names


def _save_final_ozon_documents(*, request, order: ShippingOrder) -> list[str]:
    """Persist files selected for a final Ozon submission, never for a draft/autosave."""
    from .models import ShippingOrderAttachment

    uploaded_by = request.user if request.user.is_authenticated else None
    uploaded_names: list[str] = []
    for uploaded_file in request.FILES.getlist("documents"):
        filename = str(getattr(uploaded_file, "name", "") or "").strip()
        if not filename:
            continue
        ShippingOrderAttachment.objects.create(
            order=order,
            uploaded_by=uploaded_by,
            file=uploaded_file,
        )
        uploaded_names.append(filename)
    return uploaded_names


def _create_ozon_batch_shipping_orders(
    *,
    request,
    form,
    selected_client,
    stock_rows: list[dict],
    ozon_result,
    scope: str,
    edit_order,
) -> ShippingOrder:
    """Create one ShippingOrder with Ozon destinations (no child SO per warehouse)."""
    from .distribution import refresh_order_distribution_status, validate_ozon_distribution_for_submit
    from .models import ShippingDestination, ShippingDestinationItem, ShippingOrderAttachment
    from .ozon_template import (
        attachment_content_file,
        build_ozon_parent_comment,
        items_from_selected_boxes,
    )

    groups = ozon_result.applied_groups
    if not groups:
        raise ValidationError("По шаблону Ozon нет складов с собранными позициями.")

    warehouses = [g.warehouse for g in groups]
    user_comment = str(form.cleaned_data.get("comment") or "")
    supply_number = str(
        form.cleaned_data.get("supply_number")
        or form.cleaned_data.get("wb_supply_barcode")
        or ""
    ).strip()
    transit_address = str(form.cleaned_data.get("transit_address") or "").strip()
    proto = form.save(commit=False)

    skip_fields = {
        "id",
        "pk",
        "number",
        "status",
        "created_at",
        "updated_at",
        "reserved_at",
        "expected_boxes",
        "destination_warehouse",
        "comment",
        "shipping_barcode",
        "wb_supply_barcode",
        "supply_number",
        "distribution_status",
        "parent",
        "shipped_at",
    }

    # Build per-warehouse item rows, then aggregate totals for one order.
    per_group_rows: list[tuple[object, list[dict], int]] = []
    aggregated: dict[tuple, dict] = {}
    for group in groups:
        selected_rows, box_count, item_errors = items_from_selected_boxes(
            stock_rows,
            group.selected_boxes,
            gm_by_product_barcode=group.gm_by_product_barcode(),
        )
        if item_errors and not selected_rows:
            raise ValidationError(item_errors)
        per_group_rows.append((group, selected_rows, int(max(box_count, 0))))
        for row in selected_rows:
            key = (
                str(row.get("sku_code") or "").strip(),
                str(row.get("name") or "").strip(),
                str(row.get("size") or "").strip(),
                str(row.get("barcode") or "").strip(),
                str(row.get("goods_type") or "").strip(),
            )
            existing = aggregated.get(key)
            if existing is None:
                aggregated[key] = dict(row)
            else:
                existing["qty_requested"] = int(existing.get("qty_requested") or 0) + int(
                    row.get("qty_requested") or 0
                )
                # Rebuild comment box total from merged qty when possible.
                comment = str(row.get("comment") or "")
                existing_comment = str(existing.get("comment") or "")
                box_qty = 0
                if "кратность:" in existing_comment:
                    try:
                        box_qty = int(existing_comment.split("кратность:", 1)[1].split(";", 1)[0].strip())
                    except (TypeError, ValueError):
                        box_qty = 0
                if box_qty > 0:
                    boxes = int(existing["qty_requested"]) // box_qty
                    gm_part = ""
                    for src in (existing_comment, comment):
                        if "ШК ГМ Ozon:" in src:
                            gm_part = "; " + src.split("ШК ГМ Ozon:", 1)[1].strip()
                            if not gm_part.startswith("; ШК"):
                                gm_part = f"; ШК ГМ Ozon: {gm_part.lstrip('; ').removeprefix('ШК ГМ Ozon:').strip()}"
                            break
                    existing["comment"] = f"Коробов: {boxes}; кратность: {box_qty}{gm_part}"

    if not aggregated:
        raise ValidationError("По шаблону Ozon нет позиций для заявки.")

    with transaction.atomic():
        if edit_order is not None and edit_order.status == ShippingOrder.STATUS_DRAFT:
            edit_order.status = ShippingOrder.STATUS_CANCELED
            edit_order.save(update_fields=["status", "updated_at"])
            release_client_shipping_reserve_on_cancel(edit_order, request.user)
            edit_order.items.all().delete()
            edit_order.destinations.all().delete()

        order = _copy_shipping_proto_fields(proto, skip=skip_fields)
        order.number = next_shipping_number()
        order.created_by = request.user
        order.agency = selected_client
        order.status = ShippingOrder.STATUS_SUBMITTED
        order.parent = None
        # Summary destination for list fallback; detailed split lives in destinations.
        order.destination_warehouse = ", ".join(warehouses[:4]) + (
            f" +{len(warehouses) - 4}" if len(warehouses) > 4 else ""
        )
        order.expected_boxes = int(sum(int(g.boxes_total or 0) for g in groups))
        order.supply_number = supply_number
        order.wb_supply_barcode = ""
        order.shipping_barcode = str(form.cleaned_data.get("shipping_barcode") or "").strip()
        if transit_address:
            order.wb_transit_warehouse = True
            order.transit_address = transit_address
        order.comment = build_ozon_parent_comment(
            user_comment=user_comment,
            batch_id=ozon_result.batch_id,
            warehouses=warehouses,
            groups=groups,
        )
        _apply_ozon_eta_fields(order)
        order.save()

        item_by_key: dict[tuple, ShippingOrderItem] = {}
        for key, row in aggregated.items():
            item = ShippingOrderItem.objects.create(order=order, **row)
            item_by_key[key] = item

        for sort_order, (group, selected_rows, box_count) in enumerate(per_group_rows):
            dest = ShippingDestination.objects.create(
                order=order,
                warehouse_name=group.warehouse,
                planned_boxes=int(box_count),
                planned_units=int(group.qty_total or 0),
                sort_order=sort_order,
                comment=(
                    f"ШК ГМ: {', '.join(group.gm_barcodes())}" if group.gm_barcodes() else ""
                ),
            )
            units = 0
            for row in selected_rows:
                key = (
                    str(row.get("sku_code") or "").strip(),
                    str(row.get("name") or "").strip(),
                    str(row.get("size") or "").strip(),
                    str(row.get("barcode") or "").strip(),
                    str(row.get("goods_type") or "").strip(),
                )
                order_item = item_by_key.get(key)
                if order_item is None:
                    continue
                qty = int(row.get("qty_requested") or 0)
                if qty <= 0:
                    continue
                ShippingDestinationItem.objects.create(
                    destination=dest,
                    order_item=order_item,
                    quantity=qty,
                )
                units += qty
            dest.planned_units = units or int(group.qty_total or 0)
            dest.save(update_fields=["planned_units", "updated_at"])

        refresh_order_distribution_status(order, save=True)
        validate_ozon_distribution_for_submit(order)

        parent_file = attachment_content_file(ozon_result)
        if parent_file is not None:
            ShippingOrderAttachment.objects.create(
                order=order,
                uploaded_by=request.user if request.user.is_authenticated else None,
                file=parent_file,
            )
        _save_final_ozon_documents(request=request, order=order)
        apply_client_shipping_reserve_on_submit(order, request.user)
        ensure_manager_review_task(order, request.user)
        log_order_action(
            action="create",
            order_id=order.number,
            order_type="shipping",
            user=request.user,
            agency=order.agency,
            description=(
                f"Создана заявка Ozon {ozon_result.batch_id} "
                f"с распределением на {len(groups)} конечных складов"
            ),
            payload=order_payload(
                order,
                extra={
                    "ozon_batch_id": ozon_result.batch_id,
                    "warehouses": warehouses,
                    "is_ozon_distribution": True,
                    "destination_count": len(groups),
                },
            ),
        )
    return order


def build_shipping_create_page_context(
    *,
    form,
    stock_rows: list[dict],
    parse_errors: list[str],
    selected_client,
    scope: str,
    role: str | None,
    edit_order,
    request,
    import_discrepancies: list[str] | None = None,
    import_assembled_lines: list | None = None,
    import_assembled_by_sku: list | None = None,
    ozon_preview_groups: list | None = None,
    ozon_batch_id: str = "",
    partial_stock_rows: list[dict] | None = None,
) -> dict:
    from . import views as shipping_views

    edit_returns_to_draft = (
        edit_order is not None
        and scope == "client"
        and edit_order.status == ShippingOrder.STATUS_SUBMITTED
    )
    stock_available_boxes = 0
    stock_available_qty = 0
    stock_available_lines = 0
    for row in stock_rows or []:
        try:
            boxes = int(row.get("available_boxes") or 0)
        except (TypeError, ValueError):
            boxes = 0
        try:
            qty = int(row.get("available_qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if boxes > 0 or qty > 0:
            stock_available_lines += 1
        stock_available_boxes += max(boxes, 0)
        stock_available_qty += max(qty, 0)

    use_client_lk_form = _shipping_use_client_lk_form(
        scope=scope,
        request=request,
        selected_client=selected_client,
    )
    client_stock_input_mode = str(
        request.POST.get("stock_input_mode") if getattr(request, "method", "") == "POST" else ""
    ).strip() or "quantity"
    if import_assembled_lines or ozon_preview_groups or str(
        request.POST.get("ozon_supply_payload_json")
        if getattr(request, "method", "") == "POST"
        else ""
    ).strip():
        client_stock_input_mode = "legacy"
    client_quantity_rows = (
        _client_quantity_rows_for_context(
            stock_rows=stock_rows,
            request=request,
            edit_order=edit_order,
        )
        if use_client_lk_form
        else []
    )
    client_quantity_piece_qty = sum(
        max(int(row.get("planned_piece_qty") or 0), 0)
        for row in client_quantity_rows
    )
    server_now = timezone.localtime()
    client_marketplace_min_ship_date = ""
    if scope == "client":
        lead_days = 1 if server_now.hour < shipping_views.NEXT_DAY_DEADLINE_HOUR else 2
        client_marketplace_min_ship_date = (server_now.date() + timedelta(days=lead_days)).isoformat()

    # Keep the edit form stable even when the model datetime is stored in UTC.
    # A browser date input needs a plain local YYYY-MM-DD value; parsing the
    # hidden datetime in JavaScript can otherwise display the previous day.
    initial_eta_date = ""
    if getattr(request, "method", "") == "POST":
        posted_eta = str(request.POST.get("eta_at") or "").strip()
        posted_eta_match = re.match(r"^(\d{4}-\d{2}-\d{2})", posted_eta)
        if posted_eta_match:
            initial_eta_date = posted_eta_match.group(1)
    if not initial_eta_date and edit_order is not None:
        planned_ship_date = getattr(edit_order, "planned_ship_date", None)
        if planned_ship_date is not None:
            initial_eta_date = planned_ship_date.isoformat()
        else:
            eta_at = getattr(edit_order, "eta_at", None)
            if eta_at is not None:
                if timezone.is_naive(eta_at):
                    eta_at = timezone.make_aware(eta_at, timezone.get_current_timezone())
                initial_eta_date = timezone.localtime(eta_at).date().isoformat()

    initial_selection_position_count = 0
    initial_selection_box_count = 0
    initial_selection_item_count = 0
    ozon_api_summary = _ozon_api_summary_for_form(edit_order)
    if getattr(request, "method", "") == "POST":
        posted_ozon_summary = _ozon_api_summary_from_request(request)
        if posted_ozon_summary.get("show"):
            ozon_api_summary = posted_ozon_summary
    if getattr(request, "method", "") == "POST" and ozon_api_summary.get("show"):
        posted_payload = (
            ozon_api_summary.get("payload")
            if isinstance(ozon_api_summary.get("payload"), dict)
            else {}
        )
        posted_composition = posted_payload.get("composition")
        if not isinstance(posted_composition, list):
            posted_composition = (
                posted_payload.get("items")
                if isinstance(posted_payload.get("items"), list)
                else []
            )
        positive_composition = [
            row
            for row in posted_composition
            if isinstance(row, dict) and _positive_int(row.get("quantity")) > 0
        ]
        initial_selection_position_count = len(positive_composition)
        initial_selection_item_count = sum(
            _positive_int(row.get("quantity")) for row in positive_composition
        )
        selected_boxes = (
            posted_payload.get("selected_boxes")
            if isinstance(posted_payload.get("selected_boxes"), dict)
            else {}
        )
        initial_selection_box_count = sum(
            _positive_int(value) for value in selected_boxes.values()
        )
        initial_selection_box_count += sum(
            _positive_int(split.get("boxes"))
            for split in posted_payload.get("partial_box_splits") or []
            if isinstance(split, dict)
        )
        if initial_selection_box_count <= 0:
            initial_selection_box_count = max(
                _positive_int(posted_payload.get("ozon_boxes_total")),
                _positive_int(ozon_api_summary.get("boxes_total")),
            )
    elif edit_order is not None:
        edit_items = list(edit_order.items.all())
        initial_selection_position_count = len(edit_items)
        initial_selection_item_count = sum(
            max(int(getattr(item, "qty_requested", 0) or 0), 0)
            for item in edit_items
        )
        initial_selection_box_count = max(int(getattr(edit_order, "expected_boxes", 0) or 0), 0)
        if initial_selection_box_count <= 0:
            initial_selection_box_count = sum(
                max(_box_count_from_comment(getattr(item, "comment", "")), 0)
                for item in edit_items
            )
    context = {
        "form": form,
        "marketplace_warehouse_catalog": shipping_views.load_marketplace_warehouse_catalog(),
        "stock_rows": stock_rows,
        "client_quantity_rows": client_quantity_rows,
        "client_quantity_piece_qty": client_quantity_piece_qty,
        "client_stock_input_mode": client_stock_input_mode,
        "stock_available_lines": stock_available_lines,
        "stock_available_boxes": stock_available_boxes,
        "stock_available_qty": stock_available_qty,
        "partial_stock_rows": list(partial_stock_rows or []),
        "parse_errors": parse_errors,
        "import_discrepancies": list(import_discrepancies or []),
        "import_assembled_lines": list(import_assembled_lines or []),
        "import_assembled_by_sku": list(import_assembled_by_sku or []),
        "ozon_preview_groups": list(ozon_preview_groups or []),
        "ozon_batch_id": str(ozon_batch_id or ""),
        "ozon_api_summary": ozon_api_summary,
        "ozon_supplies_url": (
            reverse("shipping:ozon-supplies")
            + (f"?client={int(selected_client.id)}" if selected_client is not None else "")
        ),
        "ozon_supplies_batch_url": (
            reverse("shipping:ozon-supplies-batch")
            + (f"?client={int(selected_client.id)}" if selected_client is not None else "")
        ),
        "ozon_supplies_detail_url_template": (
            reverse("shipping:ozon-supplies-detail", kwargs={"order_id": 0}).replace("/0/", "/{id}/")
            + (f"?client={int(selected_client.id)}" if selected_client is not None else "")
        ),
        "ozon_gm_preload_url_template": (
            reverse("shipping:ozon-gm-preload", kwargs={"order_id": 0}).replace("/0/", "/{id}/")
            + (f"?client={int(selected_client.id)}" if selected_client is not None else "")
        ),
        "existing_attachments": list(shipping_views._active_shipping_attachments(edit_order)) if edit_order else [],
        "scope": scope,
        "role": role,
        "selected_client": selected_client,
        "use_client_lk_form": use_client_lk_form,
        "cabinet_url": (
            f"/client/dashboard/lk/?client={int(selected_client.id)}"
            if use_client_lk_form and selected_client is not None
            else resolve_cabinet_url(role)
        ),
        "server_now_iso": server_now.isoformat(),
        "client_marketplace_min_ship_date": client_marketplace_min_ship_date,
        "initial_eta_date": initial_eta_date,
        "initial_selection_position_count": initial_selection_position_count,
        "initial_selection_box_count": initial_selection_box_count,
        "initial_selection_item_count": initial_selection_item_count,
        "next_day_deadline_hour": shipping_views.NEXT_DAY_DEADLINE_HOUR,
        "next_day_deadline_error": shipping_views.NEXT_DAY_DEADLINE_ERROR,
        "workday_start_hour": shipping_views.WORKDAY_START_HOUR,
        "workday_end_hour": shipping_views.WORKDAY_END_HOUR,
        "workday_hours_error": shipping_views.WORKDAY_HOURS_ERROR,
        "attachment_retention_days": shipping_views.ShippingOrderAttachment.RETENTION_DAYS,
        "edit_order": edit_order,
        "is_edit_mode": bool(edit_order),
        "page_title": f"Редактирование заявки {edit_order.number}" if edit_order else "Новая заявка на отгрузку",
        "page_subtitle": (
            "Измените параметры и позиции заявки. После сохранения она вернется в подготовку."
            if edit_returns_to_draft
            else (
                "Измените параметры и позиции заявки. После сохранения она останется на проверке у менеджера."
                if edit_order
                else (
                    "Заполните параметры — затем выберите товар из доступного к отгрузке ниже."
                    if use_client_lk_form
                    else "Создайте заявку и отправьте в работу менеджеру/складу."
                )
            )
        ),
    }
    if getattr(request, "method", "") == "POST":
        context["partial_box_splits_json"] = shipping_views._partial_box_splits_json_from_request(request)
    elif edit_order is not None:
        context["partial_box_splits_json"] = shipping_views._partial_box_splits_json_for_order(edit_order, stock_rows)
    else:
        context["partial_box_splits_json"] = "[]"
    if edit_order is not None:
        context["page_title"] = f"Редактирование заявки {shipping_views._display_shipping_number(edit_order.number)}"
    return context


def handle_shipping_create_request(
    *,
    request,
    scope: str,
    role: str | None,
    client_agency,
):
    from . import views as shipping_views

    agency_queryset = _staff_client_queryset_for_role(role)
    selected_client = client_agency if scope == "client" else None
    initial_data = {}
    edit_flag = str(request.GET.get("edit") or request.POST.get("edit") or "").strip().lower() in {"1", "true", "yes"}
    edit_order_id = str(request.GET.get("order") or request.POST.get("edit_order_id") or "").strip()
    edit_order = None
    if edit_order_id.isdigit() and (edit_flag or request.method == "POST"):
        edit_order = get_object_or_404(
            ShippingOrder.objects.select_related("agency", "created_by", "marketplace").prefetch_related("items", "reserves"),
            pk=int(edit_order_id),
        )
        if scope == "client" and client_agency is not None and edit_order.agency_id != client_agency.id:
            return HttpResponseForbidden("Доступ запрещен")
        if scope == "client" and client_agency is not None:
            from client_cabinet.request_ownership import (
                PORTAL_REQUEST_OWNER_MESSAGE,
                portal_user_can_edit_request,
            )

            if not portal_user_can_edit_request(
                user=request.user,
                agency=client_agency,
                order_type="shipping",
                order_id=edit_order.number,
                request=request,
            ):
                return HttpResponseForbidden(PORTAL_REQUEST_OWNER_MESSAGE)
        if not shipping_views._can_edit_order_form(scope, role, edit_order):
            return HttpResponseForbidden("Редактирование заявки недоступно.")
        selected_client = edit_order.agency
        initial_data["agency"] = edit_order.agency
    if scope == "staff":
        client_id = str(request.GET.get("client") or request.POST.get("client") or "").strip()
        if client_id.isdigit():
            selected = agency_queryset.filter(id=int(client_id)).first()
            if selected:
                initial_data["agency"] = selected
                selected_client = selected
    use_client_lk_form = _shipping_use_client_lk_form(
        scope=scope,
        request=request,
        selected_client=selected_client,
    )
    if (
        use_client_lk_form
        and selected_client is not None
        and edit_order is None
        and request.method == "GET"
        and not str(request.GET.get("force_new") or "").strip()
    ):
        from client_cabinet.client_drafts import client_draft_limit_reached, find_client_draft

        draft = find_client_draft(
            agency=selected_client,
            order_type="shipping",
            user=request.user,
        )
        draft_shipping_pk = draft.get("shipping_pk") if draft else None
        draft_is_ozon = bool(
            draft_shipping_pk
            and ShippingOrder.objects.select_related("marketplace")
            .filter(pk=draft_shipping_pk, agency=selected_client, status=ShippingOrder.STATUS_DRAFT)
            .filter(delivery_type=ShippingOrder.DELIVERY_MARKETPLACE, marketplace__name__iexact="Ozon")
            .exists()
        )
        if (
            draft
            and not draft_is_ozon
            and draft.get("continue_url")
            and client_draft_limit_reached(
                agency=selected_client,
                order_type="shipping",
                user=request.user,
            )
        ):
            return redirect(draft["continue_url"])
    if edit_order is not None:
        selected_client = edit_order.agency
        initial_data["agency"] = edit_order.agency
        if request.method == "GET" and not str(getattr(edit_order, "destination_warehouse", "") or "").strip():
            ozon_initial_summary = _ozon_api_summary_for_form(edit_order)
            ozon_initial_destination = str(
                ozon_initial_summary.get("destination_warehouse")
                or ozon_initial_summary.get("transit_address")
                or ""
            ).strip()
            if ozon_initial_destination:
                initial_data["destination_warehouse"] = ozon_initial_destination
    autosave = str(request.POST.get("draft_autosave") or "").strip() == "1"
    action_name = str(request.POST.get("action") or "").strip()
    import_template = action_name == "import_template"
    import_ozon_template = action_name == "import_ozon_template"
    save_as_draft_pref = autosave or action_name == "save_draft"
    posted_ozon = _request_posts_ozon_shipping(request) or _is_current_ozon_shipping(edit_order)
    ozon_rework_eta_normalized = _normalize_client_ozon_rework_eta_post(
        request,
        scope=scope,
        edit_order=edit_order,
        action_name=action_name,
        autosave=autosave,
    )
    posted_marketplace_without_selection = _request_posts_marketplace_without_selection(request)
    confirmed_draft_switch_to_marketplace = bool(
        str(request.POST.get("reset_draft_on_type_change") or "").strip() == "1"
        and edit_order is not None
        and edit_order.status == ShippingOrder.STATUS_DRAFT
        and edit_order.delivery_type != ShippingOrder.DELIVERY_MARKETPLACE
        and posted_marketplace_without_selection
    )
    if (
        request.method == "POST"
        and use_client_lk_form
        and save_as_draft_pref
        and posted_marketplace_without_selection
        and not posted_ozon
        and not confirmed_draft_switch_to_marketplace
    ):
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    "Сначала выберите маркетплейс. "
                    "Для Ozon черновики не создаются."
                ),
            },
            status=400,
        )
    if request.method == "POST" and save_as_draft_pref and posted_ozon:
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    "Для Ozon сохранение черновиков отключено. "
                    "Данные сохранятся только после отправки заявки."
                ),
            },
            status=400,
        )
    import_discrepancies: list[str] = []
    ozon_preview_groups: list = []
    ozon_batch_id = ""
    if (
        use_client_lk_form
        and selected_client is not None
        and edit_order is None
        and request.method == "POST"
        and save_as_draft_pref
        and not str(
            request.GET.get("force_new")
            or request.POST.get("force_new")
            or ""
        ).strip()
    ):
        from client_cabinet.client_drafts import client_draft_limit_reached, find_client_draft

        draft = None
        if client_draft_limit_reached(
            agency=selected_client,
            order_type="shipping",
            user=request.user,
        ):
            draft = find_client_draft(
                agency=selected_client,
                order_type="shipping",
                user=request.user,
            )
        shipping_pk = draft.get("shipping_pk") if draft else None
        if shipping_pk:
            edit_order = (
                ShippingOrder.objects.select_related("agency", "created_by", "marketplace")
                .prefetch_related("items", "reserves")
                .filter(pk=int(shipping_pk), agency=selected_client, status=ShippingOrder.STATUS_DRAFT)
                .first()
            )
            if edit_order is not None:
                selected_client = edit_order.agency
                initial_data["agency"] = edit_order.agency

    stock_rows: list[dict] = []
    partial_stock_rows: list[dict] = []
    if selected_client is not None:
        stock_rows = shipping_views._shipping_stock_picker_rows(
            selected_client,
            exclude_order=edit_order,
            diagnostics=partial_stock_rows,
        )

    use_client_lk_form = _shipping_use_client_lk_form(
        scope=scope,
        request=request,
        selected_client=selected_client,
    )
    locked_agency = selected_client if use_client_lk_form else None

    # Client Excel template import: validate availability/multiplicity, force draft on gaps.
    if request.method == "POST" and import_template and use_client_lk_form and selected_client is not None:
        from .client_template import (
            apply_client_shipping_template,
            merge_comment_with_discrepancies,
        )

        def _render_client_import_form(
            *,
            errors: list[str],
            discrepancies: list[str],
            assembled: list | None = None,
            assembled_by_sku: list | None = None,
        ):
            form_local = shipping_views.ShippingOrderForm(
                request.POST or None,
                request.FILES or None,
                instance=edit_order,
                initial=initial_data,
                agency_queryset=agency_queryset,
                locked_agency=locked_agency,
                relax_for_autosave=False,
                allow_marketplace_date_override=scope == "staff",
            )
            rows_local = shipping_views._with_selected_boxes(
                stock_rows,
                shipping_views._selected_box_values_from_request(request),
            )
            return render(
                request,
                "shipping/form_client_lk.html",
                build_shipping_create_page_context(
                    form=form_local,
                    stock_rows=rows_local,
                    parse_errors=errors,
                    selected_client=selected_client,
                    scope=scope,
                    role=role,
                    edit_order=edit_order,
                    request=request,
                    import_discrepancies=discrepancies,
                    import_assembled_lines=assembled or [],
                    import_assembled_by_sku=assembled_by_sku or [],
                ),
            )

        upload = request.FILES.get("shipping_template")
        if not upload:
            return _render_client_import_form(
                errors=["Выберите файл Excel-шаблона."],
                discrepancies=[],
            )

        import_result = apply_client_shipping_template(file_obj=upload, stock_rows=stock_rows)
        import_discrepancies = list(import_result.discrepancies)
        assembled_lines = list(import_result.assembled_lines)
        assembled_by_sku = list(import_result.assembled_by_sku)
        if import_result.parse_errors:
            return _render_client_import_form(
                errors=list(import_result.parse_errors),
                discrepancies=import_discrepancies,
                assembled=assembled_lines,
                assembled_by_sku=assembled_by_sku,
            )

        _inject_stock_box_selections(
            request,
            stock_rows,
            import_result.selected_boxes,
            partial_box_splits=import_result.partial_box_splits,
        )
        post = request.POST.copy()
        block = import_result.discrepancy_comment_block()
        if block:
            post["comment"] = merge_comment_with_discrepancies(post.get("comment"), block)
        # Stay on the form so assembled positions are visible immediately.
        post["action"] = ""
        request.POST = post

        if import_result.applied_lines:
            if import_result.has_discrepancies:
                messages.warning(
                    request,
                    "Шаблон загружен: собраны целые короба и поштучный добор по остаткам. "
                    "Смотрите блок «Собрано» и расхождения — при необходимости подправьте вручную.",
                )
            else:
                messages.success(
                    request,
                    "Шаблон загружен. Ниже — артикулы, короба и штуки к отгрузке. Проверьте и отправьте заявку.",
                )
            return _render_client_import_form(
                errors=[],
                discrepancies=import_discrepancies,
                assembled=assembled_lines,
                assembled_by_sku=assembled_by_sku,
            )

        fail_errors = [
            "По шаблону не удалось заполнить ни одной позиции.",
            "Укажите баркод товара и количество в штуках — система сама наберёт целые короба и поштучный добор. "
            "Либо заполните столбец «Коробов».",
        ]
        if import_discrepancies:
            fail_errors.append("Причины по строкам:")
            fail_errors.extend(import_discrepancies)
        return _render_client_import_form(
            errors=fail_errors,
            discrepancies=import_discrepancies,
            assembled=assembled_lines,
            assembled_by_sku=assembled_by_sku,
        )

    # Ozon Excel: preview groups by destination warehouse (N orders created on submit).
    if request.method == "POST" and import_ozon_template and use_client_lk_form and selected_client is not None:
        from .ozon_template import (
            apply_ozon_shipping_template_by_warehouse,
            session_key_for_agency,
        )

        def _render_ozon_import_form(
            *,
            errors: list[str],
            discrepancies: list[str],
            groups: list | None = None,
            batch_id: str = "",
        ):
            form_local = shipping_views.ShippingOrderForm(
                request.POST or None,
                request.FILES or None,
                instance=edit_order,
                initial=initial_data,
                agency_queryset=agency_queryset,
                locked_agency=locked_agency,
                relax_for_autosave=False,
                allow_marketplace_date_override=scope == "staff",
            )
            return render(
                request,
                "shipping/form_client_lk.html",
                build_shipping_create_page_context(
                    form=form_local,
                    stock_rows=stock_rows,
                    parse_errors=errors,
                    selected_client=selected_client,
                    scope=scope,
                    role=role,
                    edit_order=edit_order,
                    request=request,
                    import_discrepancies=discrepancies,
                    ozon_preview_groups=groups or [],
                    ozon_batch_id=batch_id,
                ),
            )

        upload = request.FILES.get("shipping_ozon_template") or request.FILES.get("shipping_template")
        if not upload:
            return _render_ozon_import_form(
                errors=["Выберите Excel-шаблон Ozon."],
                discrepancies=[],
            )

        ozon_result = apply_ozon_shipping_template_by_warehouse(
            file_obj=upload,
            stock_rows=stock_rows,
            file_name=getattr(upload, "name", "") or "ozon.xlsx",
        )
        if ozon_result.parse_errors:
            return _render_ozon_import_form(
                errors=list(ozon_result.parse_errors),
                discrepancies=ozon_result.all_discrepancies(),
            )

        applied = ozon_result.applied_groups
        request.session[session_key_for_agency(int(selected_client.id))] = ozon_result.to_session_dict()
        request.session.modified = True
        post = request.POST.copy()
        post["action"] = ""
        request.POST = post

        preview = [g.to_dict() for g in ozon_result.groups]
        disc = ozon_result.all_discrepancies()
        if applied:
            if ozon_result.has_discrepancies:
                messages.warning(
                    request,
                    f"Шаблон Ozon: будет создана одна заявка с распределением на {len(applied)} конечных складов. "
                    "Есть расхождения — проверьте превью перед отправкой.",
                )
            else:
                messages.success(
                    request,
                    f"Шаблон Ozon разобран: {len(applied)} конечных склад(ов). "
                    "Укажите транзитный склад и номер поставки, затем «Отправить менеджеру» — "
                    "создастся одна заявка с распределением.",
                )
            return _render_ozon_import_form(
                errors=[],
                discrepancies=disc,
                groups=preview,
                batch_id=ozon_result.batch_id,
            )

        fail_errors = [
            "По шаблону Ozon не удалось заполнить ни одной позиции ни для одного склада.",
        ]
        if disc:
            fail_errors.append("Причины:")
            fail_errors.extend(disc)
        return _render_ozon_import_form(
            errors=fail_errors,
            discrepancies=disc,
            groups=preview,
            batch_id=ozon_result.batch_id,
        )

    quantity_selection_errors: list[str] = []
    if use_client_lk_form and _client_quantity_mode_requested(request):
        quantity_selection_errors = _materialize_client_quantity_selection(
            request,
            stock_rows,
        )

    if (
        autosave
        and edit_order is not None
        and edit_order.status != ShippingOrder.STATUS_DRAFT
    ):
        return JsonResponse(
            {"ok": False, "error": "Автосохранение доступно только для черновиков."},
            status=400,
        )
    editing_current_ozon = _is_current_ozon_shipping(edit_order)
    original_delivery_type = str(
        getattr(edit_order, "delivery_type", "") or ""
    ).strip()
    draft_type_reset_requested = (
        str(request.POST.get("reset_draft_on_type_change") or "").strip() == "1"
    )
    ozon_live_source_refreshed, ozon_live_source_error = (
        _refresh_client_ozon_post_from_api(
            request,
            scope=scope,
            selected_client=selected_client,
            edit_order=edit_order,
            action_name=action_name,
            autosave=autosave,
            stock_rows=stock_rows,
        )
    )
    form = shipping_views.ShippingOrderForm(
        request.POST or None,
        request.FILES or None,
        instance=edit_order,
        initial=initial_data,
        agency_queryset=agency_queryset,
        locked_agency=locked_agency,
        relax_for_autosave=autosave or (import_template and save_as_draft_pref),
        ozon_multi_warehouse=False,
        allow_marketplace_date_override=scope == "staff",
    )
    parse_errors: list[str] = list(quantity_selection_errors)
    if ozon_live_source_error:
        parse_errors.append(ozon_live_source_error)

    # Restore Ozon multi-warehouse preview from session (after «Применить Ozon»).
    cached_ozon = None
    if use_client_lk_form and selected_client is not None:
        from .ozon_template import OzonMultiImportResult, session_key_for_agency

        cached_ozon = OzonMultiImportResult.from_session_dict(
            request.session.get(session_key_for_agency(int(selected_client.id)))
        )
        if cached_ozon is not None:
            ozon_preview_groups = [g.to_dict() for g in cached_ozon.groups]
            ozon_batch_id = cached_ozon.batch_id
            import_discrepancies = list(cached_ozon.all_discrepancies())
            if cached_ozon.applied_groups:
                form.ozon_multi_warehouse = True

    if request.method == "POST":
        stock_rows = shipping_views._with_selected_boxes(stock_rows, shipping_views._selected_box_values_from_request(request))
    elif edit_order is not None:
        stock_rows = shipping_views._with_selected_boxes(stock_rows, shipping_views._selected_box_values_for_order(edit_order, stock_rows))
    else:
        stock_rows = shipping_views._with_selected_boxes(stock_rows)

    def autosave_response(*, ok: bool, order=None, error: str = "", status: int = 200):
        if not autosave:
            return None
        payload = {"ok": ok}
        if error:
            payload["error"] = error
        if order is not None:
            payload["order_id"] = order.pk
            payload["draft_order_id"] = order.pk
            payload["number"] = order.number
            payload["status"] = order.status
        return JsonResponse(payload, status=status if ok else 400)

    if request.method == "POST":
        from .client_template import DISCREPANCY_MARKER

        save_as_draft = autosave or request.POST.get("action") == "save_draft"
        # Template gaps require manager review — do not allow final submit with unresolved gaps.
        if (
            use_client_lk_form
            and not save_as_draft
            and DISCREPANCY_MARKER in str(request.POST.get("comment") or "")
        ):
            if posted_ozon:
                parse_errors.append(
                    "Заявка Ozon не отправлена и не сохранена: "
                    "исправьте расхождения по составу и повторите отправку."
                )
            else:
                save_as_draft = True
                messages.warning(
                    request,
                    "В заявке есть расхождения по шаблону — сохранено черновиком для корректировки менеджером.",
                )
        form_valid = form.is_valid()
        cross_type_edit_error = ""
        submitted_delivery_type = ""
        draft_delivery_type_changed = False
        if form_valid:
            submitted_delivery_type = str(
                form.cleaned_data.get("delivery_type") or ""
            ).strip()
            draft_delivery_type_changed = bool(
                edit_order is not None
                and edit_order.status == ShippingOrder.STATUS_DRAFT
                and original_delivery_type
                and submitted_delivery_type != original_delivery_type
            )
            if draft_delivery_type_changed and not draft_type_reset_requested:
                cross_type_edit_error = (
                    "Подтвердите изменение типа отгрузки. После подтверждения параметры "
                    "и состав черновика будут очищены; загруженные файлы останутся."
                )
        if form_valid and editing_current_ozon and not cross_type_edit_error:
            submitted_marketplace = form.cleaned_data.get("marketplace")
            submitted_marketplace_name = str(
                getattr(submitted_marketplace, "name", "") or ""
            ).strip().lower()
            submitted_is_ozon = (
                submitted_delivery_type == ShippingOrder.DELIVERY_MARKETPLACE
                and submitted_marketplace_name == "ozon"
            )
            if not submitted_is_ozon and not (
                draft_delivery_type_changed and draft_type_reset_requested
            ):
                cross_type_edit_error = (
                    "Заявку Ozon нельзя преобразовать в СДЭК или другой тип доставки. "
                    "Создайте отдельную новую заявку."
                )
        if form_valid:
            order_agency = client_agency if (scope == "client" and client_agency is not None) else form.cleaned_data.get("agency")
            marketplace = form.cleaned_data.get("marketplace")
            mp_name = str(getattr(marketplace, "name", "") or "").strip().lower()
            is_ozon_mp = mp_name == "ozon"
            ozon_api_meta = _ozon_supply_meta_from_request(request) if is_ozon_mp else {}
            ozon_audit_extra = _ozon_api_audit_extra(ozon_api_meta)
            if ozon_live_source_refreshed:
                ozon_audit_extra["ozon_live_source_refreshed"] = True
            ozon_submit = (
                use_client_lk_form
                and not save_as_draft
                and not autosave
                and is_ozon_mp
                and cached_ozon is not None
                and bool(cached_ozon.applied_groups)
            )
            if ozon_submit:
                try:
                    from .ozon_template import session_key_for_agency as _ozon_sk

                    parent_order = _create_ozon_batch_shipping_orders(
                        request=request,
                        form=form,
                        selected_client=selected_client or order_agency,
                        stock_rows=shipping_views._shipping_stock_picker_rows(
                            selected_client or order_agency,
                            exclude_order=edit_order,
                        ),
                        ozon_result=cached_ozon,
                        scope=scope,
                        edit_order=edit_order,
                    )
                    request.session.pop(_ozon_sk(int((selected_client or order_agency).id)), None)
                    request.session.modified = True
                    dest_count = parent_order.destinations.count()
                    messages.success(
                        request,
                        f"Создана заявка Ozon {parent_order.number}: "
                        f"распределение на {dest_count} конечных склад(ов).",
                    )
                    return redirect("shipping:detail", pk=parent_order.pk)
                except ValidationError as exc:
                    parse_errors.extend(list(exc.messages))
                    selected_rows = []
                    selected_box_count = 0
                except Exception as exc:
                    logger.exception("Failed to create Ozon batch shipping orders")
                    parse_errors.append(f"Не удалось создать заявки Ozon: {exc}")
                    selected_rows = []
                    selected_box_count = 0
            else:
                stock_rows = shipping_views._shipping_stock_picker_rows(order_agency, exclude_order=edit_order)
                selected_rows, selection_parse_errors = shipping_views._parse_selected_stock_items(request, stock_rows, multiple=True)
                parse_errors.extend(selection_parse_errors)
                _expanded_rows, selected_box_count, selection_errors = shipping_views._selected_stock_rows_with_boxes(
                    request,
                    stock_rows,
                    multiple=True,
                )
                selected_box_count += shipping_views._partial_box_count_from_request(request)
                if selection_errors and not save_as_draft:
                    parse_errors.extend(selection_errors)
                if is_ozon_mp and ozon_api_meta:
                    gm_reconcile_errors: list[str] = []
                    if (
                        edit_order is not None
                        and edit_order.status == ShippingOrder.STATUS_DRAFT
                        and not parse_errors
                        and not ozon_live_source_refreshed
                    ):
                        saved_exact_meta = _saved_ozon_api_intake_meta(
                            edit_order,
                            selected_rows=selected_rows,
                            prefer_exact_match=True,
                        )
                        if saved_exact_meta.get("_selected_rows_exact_match"):
                            ozon_api_meta = {**ozon_api_meta, **saved_exact_meta}
                            gm_barcodes = [
                                str(row.get("gm_barcode") or "").strip()
                                for row in _normalize_ozon_gm_cargoes(
                                    ozon_api_meta.get("gm_cargoes")
                                )
                                if str(row.get("gm_barcode") or "").strip()
                            ]
                            ozon_api_meta["gm_barcodes"] = gm_barcodes
                            ozon_api_meta["ozon_gm_comment"] = (
                                f"ШК ГМ Ozon: {', '.join(gm_barcodes)}"
                                if gm_barcodes
                                else ""
                            )
                    if not parse_errors:
                        ozon_api_meta, gm_reconcile_errors = (
                            _reconcile_ozon_gm_meta_with_selected_rows(
                                agency=order_agency,
                                meta=ozon_api_meta,
                                selected_rows=selected_rows,
                            )
                        )
                        parse_errors.extend(gm_reconcile_errors)
                    if not parse_errors:
                        selected_rows = _attach_ozon_gm_to_selected_rows(selected_rows, ozon_api_meta)
                    ozon_audit_extra = _ozon_api_audit_extra(ozon_api_meta)
                    if ozon_live_source_refreshed:
                        ozon_audit_extra["ozon_live_source_refreshed"] = True
                # Drafts (manual + autosave) may have no stock lines yet while manager fills the form.
                # Ozon preview also allows draft without picker lines (positions live in session batch).
                if save_as_draft or (
                    cached_ozon is not None and bool(cached_ozon.applied_groups) and is_ozon_mp
                ):
                    parse_errors = [
                        err
                        for err in parse_errors
                        if "минимум одну позицию" not in str(err)
                    ]
                if draft_delivery_type_changed and draft_type_reset_requested:
                    selected_rows = []
                    selected_box_count = 0
                stock_rows = shipping_views._with_selected_boxes(stock_rows, shipping_views._selected_box_values_from_request(request))
        else:
            selected_rows = []
            selected_box_count = 0
        if cross_type_edit_error and cross_type_edit_error not in parse_errors:
            parse_errors.append(cross_type_edit_error)
        if form_valid and not parse_errors and not (
            use_client_lk_form
            and not save_as_draft
            and not autosave
            and cached_ozon is not None
            and bool(cached_ozon.applied_groups)
            and str(getattr(form.cleaned_data.get("marketplace"), "name", "") or "").strip().lower() == "ozon"
        ):
            # Client correction of submitted stays with manager (no bounce back to draft).
            client_returns_submitted_to_draft = False
            saved_existing_order = edit_order is not None
            reset_draft_type_data = False
            try:
                with transaction.atomic():
                    order = form.save(commit=False)
                    order.agency = client_agency if (scope == "client" and client_agency is not None) else order_agency
                    submitted_at = timezone.localtime()
                    if is_ozon_mp and ozon_api_meta:
                        if hasattr(order, "supply_number") and not str(getattr(order, "supply_number", "") or "").strip():
                            order.supply_number = str(
                                ozon_api_meta.get("supply_number")
                                or ozon_api_meta.get("order_number")
                                or ""
                            ).strip()
                        if not str(getattr(order, "shipping_barcode", "") or "").strip():
                            order.shipping_barcode = str(
                                ozon_api_meta.get("shipping_barcode")
                                or ozon_api_meta.get("supply_number")
                                or ozon_api_meta.get("order_number")
                                or ""
                            ).strip()
                        order.comment = _comment_with_ozon_api_meta(order.comment, ozon_api_meta)
                    edit_target = edit_order
                    reused_ozon_draft = False
                    if use_client_lk_form and save_as_draft and order.agency_id:
                        # Serialize draft creation per company.  Two tabs (or a
                        # portal user and a Fullbox manager) must update the
                        # same draft instead of creating parallel requests.
                        Agency.objects.select_for_update().filter(pk=order.agency_id).only("id").first()
                        if edit_target is None:
                            from client_cabinet.client_drafts import find_client_draft

                            current_draft = find_client_draft(
                                agency=order.agency,
                                order_type="shipping",
                                user=request.user,
                            )
                            current_pk = current_draft.get("shipping_pk") if current_draft else None
                            if current_pk:
                                existing_draft = (
                                    ShippingOrder.objects.select_for_update()
                                    .filter(
                                        pk=int(current_pk),
                                        agency=order.agency,
                                        status=ShippingOrder.STATUS_DRAFT,
                                    )
                                    .first()
                                )
                                if existing_draft is not None:
                                    edit_target = existing_draft
                                    order = _copy_shipping_form_values(existing_draft, order)
                                    saved_existing_order = True
                    if is_ozon_mp:
                        # Serialize Ozon creates per client so autosave/final-submit races
                        # cannot create two active shipments for the same marketplace supply.
                        Agency.objects.select_for_update().filter(pk=order.agency_id).only("id").first()
                        duplicate_order = _find_active_ozon_supply_duplicate_for_update(order)
                        if duplicate_order is not None:
                            duplicate_value = _ozon_supply_identity_values(order)[0]
                            duplicate_label = duplicate_order.number or f"#{duplicate_order.pk}"
                            if edit_target is None and duplicate_order.status == ShippingOrder.STATUS_DRAFT:
                                edit_target = duplicate_order
                                order = _copy_shipping_form_values(duplicate_order, order)
                                reused_ozon_draft = True
                                saved_existing_order = True
                            else:
                                status_labels = dict(ShippingOrder.STATUS_CHOICES)
                                status_label = status_labels.get(duplicate_order.status, duplicate_order.status)
                                raise ValidationError(
                                    f"Поставка Ozon {duplicate_value} уже загружена в заявку "
                                    f"{duplicate_label} ({status_label}). Дубль не создан."
                                )
                    if edit_target is not None:
                        edit_target = ShippingOrder.objects.select_for_update().get(pk=edit_target.pk)
                        reset_draft_type_data = bool(
                            draft_type_reset_requested
                            and submitted_delivery_type
                            and submitted_delivery_type
                            != str(edit_target.delivery_type or "").strip()
                        )
                        if reset_draft_type_data and edit_target.status != ShippingOrder.STATUS_DRAFT:
                            raise ValidationError(
                                "Тип отгрузки можно изменить с очисткой данных только у черновика."
                            )
                        if save_as_draft and edit_target.status != ShippingOrder.STATUS_DRAFT:
                            stale_autosave_message = (
                                "Заявка уже отправлена менеджеру. "
                                "Автосохранение черновика остановлено, чтобы не вернуть заявку назад в черновик."
                            )
                            saved = autosave_response(ok=False, error=stale_autosave_message)
                            if saved:
                                return saved
                            raise ValidationError(stale_autosave_message)
                        order.number = edit_target.number
                        order.created_by = edit_target.created_by
                        if save_as_draft:
                            order.status = ShippingOrder.STATUS_DRAFT
                        elif edit_target.status == ShippingOrder.STATUS_DRAFT:
                            # Client continues a draft and finally submits it.
                            order.status = ShippingOrder.STATUS_SUBMITTED
                        else:
                            # Keep submitted while client/manager corrects before warehouse.
                            order.status = edit_target.status
                        order.expected_boxes = _submission_expected_box_count(
                            selected_box_count=selected_box_count,
                            is_ozon=is_ozon_mp,
                            ozon_api_meta=ozon_api_meta,
                        )
                        if reset_draft_type_data:
                            _clear_shipping_draft_after_delivery_type_change(order)
                        else:
                            _sync_ozon_eta_after_slot_change(order, edit_target)
                            _apply_planned_ship_date(
                                order,
                                submitted_at=(
                                    submitted_at
                                    if edit_target.status == ShippingOrder.STATUS_DRAFT
                                    else getattr(edit_target, "created_at", None) or submitted_at
                                ),
                                submitted=order.status != ShippingOrder.STATUS_DRAFT,
                            )
                        order.save()
                        if reset_draft_type_data:
                            edit_target.destinations.all().delete()
                        order.items.all().delete()
                        for row in selected_rows:
                            ShippingOrderItem.objects.create(order=order, **row)
                        if order.status == ShippingOrder.STATUS_SUBMITTED and is_ozon_mp:
                            _raise_for_strict_ozon_gm_intake(
                                order,
                                ozon_api_meta=ozon_api_meta or None,
                            )
                        if order.status == ShippingOrder.STATUS_SUBMITTED:
                            # Reserve only after send to manager (not while draft).
                            if scope == "client" or use_client_lk_form:
                                apply_client_shipping_reserve_on_submit(
                                    order,
                                    request.user,
                                    ozon_api_meta=(
                                        _ozon_meta_for_reserve(ozon_api_meta)
                                        if is_ozon_mp
                                        else None
                                    ),
                                )
                            else:
                                reserve_order(
                                    order,
                                    request.user,
                                    target_status=ShippingOrder.STATUS_SUBMITTED,
                                    log_description="Резерв обновлен после редактирования заявки менеджером",
                                    ozon_api_meta=(
                                        _ozon_meta_for_reserve(ozon_api_meta)
                                        if is_ozon_mp
                                        else None
                                    ),
                                )
                            ensure_manager_review_task(order, request.user)
                        elif order.status == ShippingOrder.STATUS_DRAFT:
                            # Draft must never keep a reserve (client LK rule).
                            if scope == "client" or use_client_lk_form:
                                release_client_shipping_reserve_on_cancel(order, request.user)
                            if use_client_lk_form:
                                from client_cabinet.client_drafts import supersede_extra_client_drafts

                                supersede_extra_client_drafts(
                                    agency=order.agency,
                                    order_type="shipping",
                                    keep_order_id=str(order.number),
                                    user=request.user,
                                )
                        # Staff form submits files with the explicit save; autosave never does.
                        # Client upload endpoints and final Ozon submission keep their rules.
                        deleted_documents = []
                        uploaded_documents = (
                            _save_final_ozon_documents(request=request, order=order)
                            if is_ozon_mp
                            and order.status == ShippingOrder.STATUS_SUBMITTED
                            and not autosave
                            else _save_staff_form_documents(request=request, order=order)
                            if scope == "staff" and shipping_views._is_manager_role(role)
                            and not use_client_lk_form and not is_ozon_mp and not autosave
                            else []
                        )
                        shipping_views._log_update(
                            order,
                            request,
                            (
                                "Заявка отредактирована клиентом"
                                if scope == "client"
                                else "Заявка отредактирована менеджером"
                            ),
                            action="update",
                            extra={
                                "documents": shipping_views._shipping_attachment_names(order),
                                "uploaded_documents": uploaded_documents,
                                "deleted_documents": deleted_documents,
                                "edit_mode": True,
                                "reused_ozon_draft": reused_ozon_draft,
                                "returned_to_draft": False,
                                "ozon_rework_eta_normalized": ozon_rework_eta_normalized,
                                "autosave": autosave,
                                "delivery_type_reset": reset_draft_type_data,
                                "previous_delivery_type": (
                                    str(edit_target.delivery_type or "").strip()
                                    if reset_draft_type_data
                                    else ""
                                ),
                                "new_delivery_type": (
                                    str(order.delivery_type or "").strip()
                                    if reset_draft_type_data
                                    else ""
                                ),
                                **ozon_audit_extra,
                            },
                        )
                    else:
                        order.number = next_shipping_number()
                        order.created_by = request.user
                        order.status = (
                            ShippingOrder.STATUS_DRAFT
                            if save_as_draft
                            else ShippingOrder.STATUS_SUBMITTED
                        )
                        order.expected_boxes = _submission_expected_box_count(
                            selected_box_count=selected_box_count,
                            is_ozon=is_ozon_mp,
                            ozon_api_meta=ozon_api_meta,
                        )
                        _apply_planned_ship_date(
                            order,
                            submitted_at=submitted_at,
                            submitted=order.status == ShippingOrder.STATUS_SUBMITTED,
                        )
                        order.save()
                        for row in selected_rows:
                            ShippingOrderItem.objects.create(order=order, **row)
                        if order.status == ShippingOrder.STATUS_SUBMITTED and is_ozon_mp:
                            _raise_for_strict_ozon_gm_intake(
                                order,
                                ozon_api_meta=ozon_api_meta or None,
                            )
                        if order.status == ShippingOrder.STATUS_SUBMITTED:
                            # Client LK: reserve only on send to manager; draft stays free.
                            if scope == "client" or use_client_lk_form:
                                apply_client_shipping_reserve_on_submit(
                                    order,
                                    request.user,
                                    ozon_api_meta=(
                                        _ozon_meta_for_reserve(ozon_api_meta)
                                        if is_ozon_mp
                                        else None
                                    ),
                                )
                            else:
                                reserve_order(
                                    order,
                                    request.user,
                                    target_status=ShippingOrder.STATUS_SUBMITTED,
                                    log_description="Резерв выполнен автоматически при отправке клиентом",
                                    ozon_api_meta=(
                                        _ozon_meta_for_reserve(ozon_api_meta)
                                        if is_ozon_mp
                                        else None
                                    ),
                                )
                        elif order.status == ShippingOrder.STATUS_DRAFT and (
                            scope == "client" or use_client_lk_form
                        ):
                            release_client_shipping_reserve_on_cancel(order, request.user)
                        # Для Ozon выбранные файлы сохраняются вместе с финальной отправкой.
                        # Черновик и автосохранение файлы Ozon не обрабатывают.
                        uploaded_documents = (
                            _save_final_ozon_documents(request=request, order=order)
                            if is_ozon_mp
                            and order.status == ShippingOrder.STATUS_SUBMITTED
                            and not autosave
                            else _save_staff_form_documents(request=request, order=order)
                            if scope == "staff" and shipping_views._is_manager_role(role)
                            and not use_client_lk_form and not is_ozon_mp and not autosave
                            else []
                        )
                        log_order_action(
                            action="create",
                            order_id=order.number,
                            order_type="shipping",
                            user=request.user,
                            agency=order.agency,
                            description="Создана заявка на отгрузку",
                            payload=order_payload(
                                order,
                                extra={
                                    "documents": shipping_views._shipping_attachment_names(order),
                                    "uploaded_documents": uploaded_documents,
                                    "autosave": autosave,
                                    **ozon_audit_extra,
                                },
                            ),
                        )
                        if order.status == ShippingOrder.STATUS_SUBMITTED:
                            ensure_manager_review_task(order, request.user)
                        elif order.status == ShippingOrder.STATUS_DRAFT and use_client_lk_form:
                            from client_cabinet.client_drafts import supersede_extra_client_drafts

                            supersede_extra_client_drafts(
                                agency=order.agency,
                                order_type="shipping",
                                keep_order_id=str(order.number),
                                user=request.user,
                            )
                if reset_draft_type_data and order.agency_id:
                    from .ozon_template import session_key_for_agency as _ozon_session_key

                    request.session.pop(_ozon_session_key(int(order.agency_id)), None)
                    request.session.modified = True
                saved = autosave_response(ok=True, order=order)
                if saved:
                    return saved
                if saved_existing_order:
                    if client_returns_submitted_to_draft:
                        messages.success(request, f"Заявка {order.number} возвращена в подготовку.")
                    else:
                        messages.success(request, f"Заявка {order.number} обновлена.")
                else:
                    messages.success(request, f"Заявка {order.number} создана.")
                if ozon_rework_eta_normalized:
                    messages.info(
                        request,
                        "Устаревшая дата отгрузки автоматически синхронизирована "
                        "с датой слота Ozon.",
                    )
                if scope == "client" or use_client_lk_form:
                    agency_id = getattr(client_agency, "id", None) or getattr(order, "agency_id", None)
                    if agency_id:
                        return redirect(f"{reverse('shipping:detail', args=[order.pk])}?client={agency_id}")
                return redirect("shipping:detail", pk=order.pk)
            except ValidationError as exc:
                parse_errors.extend(exc.messages)
            except Exception as exc:
                logger.exception("Failed to save shipping order form")
                parse_errors.append(_shipping_form_save_error_message(exc))
        elif autosave:
            error = "Не удалось сохранить черновик."
            if form.errors:
                error = "; ".join(
                    f"{field}: {', '.join(errs)}" for field, errs in form.errors.items()
                )
            elif parse_errors:
                error = "; ".join(str(item) for item in parse_errors)
            early = autosave_response(ok=False, error=error)
            if early:
                return early

        if scope == "client" and not autosave and (form.errors or parse_errors):
            rejection_reasons = [
                f"{field}: {'; '.join(str(error) for error in errors)}"
                for field, errors in form.errors.items()
            ]
            rejection_reasons.extend(str(error) for error in parse_errors)
            logger.warning(
                "Client shipping form rejected order_pk=%s action=%s reasons=%s",
                getattr(edit_order, "pk", None),
                action_name,
                rejection_reasons,
            )

    context = build_shipping_create_page_context(
        form=form,
        stock_rows=stock_rows,
        parse_errors=parse_errors,
        selected_client=selected_client,
        scope=scope,
        role=role,
        edit_order=edit_order,
        request=request,
        import_discrepancies=import_discrepancies,
        ozon_preview_groups=ozon_preview_groups,
        ozon_batch_id=ozon_batch_id,
        partial_stock_rows=partial_stock_rows,
    )
    template_name = (
        "shipping/form_client_lk.html"
        if use_client_lk_form
        else "shipping/form.html"
    )
    return render(request, template_name, context)


def handle_shipping_dispatch_act_request(*, request, pk: int):
    from . import dispatch as shipping_dispatch
    from . import selectors as shipping_selectors
    from . import views as shipping_views

    scope, role, client_agency = shipping_views._request_scope(request)
    # Staff can manage the act; portal clients may view their own order's act.
    if scope not in {"staff", "client"}:
        return HttpResponseForbidden("Доступ запрещен")
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "created_by", "marketplace").prefetch_related("items", "reserves"),
        pk=pk,
    )
    if not shipping_views._can_access_order(scope, client_agency, order):
        return HttpResponseForbidden("Доступ запрещен")
    if order.status not in {
        ShippingOrder.STATUS_PACKED,
        ShippingOrder.STATUS_SHIPPED,
        ShippingOrder.STATUS_PARTIAL,
    }:
        if scope == "client":
            from client_cabinet.services import build_client_cabinet_url

            return redirect(
                f"{build_client_cabinet_url(order.agency_id)}#/request/shipping/{order.number}"
            )
        return redirect("shipping:detail", pk=order.pk)
    context = shipping_selectors.build_shipping_dispatch_context(
        order,
        role=role if scope == "staff" else "client",
        sign_status=request.GET.get("signed") or "",
        sign_error=request.GET.get("error") or "",
    )
    return render(request, "shipping/dispatch_act.html", context)


def handle_shipping_dispatch_sign_logistician_request(*, request, pk: int):
    from . import dispatch as shipping_dispatch
    from . import views as shipping_views

    if request.method != "POST":
        return HttpResponseForbidden("Доступ запрещен")
    scope, role, client_agency = shipping_views._request_scope(request)
    if scope != "staff" or not shipping_views._is_logistician_role(role):
        return HttpResponseForbidden("Доступ запрещен")
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "created_by", "marketplace").prefetch_related("items", "reserves"),
        pk=pk,
    )
    if not shipping_views._can_access_order(scope, client_agency, order):
        return HttpResponseForbidden("Доступ запрещен")
    packing_summary = shipping_views._shipping_packing_summary(order)
    dispatch_stage = shipping_dispatch.shipping_dispatch_stage(order, packing_summary=packing_summary)
    try:
        shipping_dispatch.sign_dispatch_act_logistician(
            order,
            request.user,
            dispatch_stage=dispatch_stage,
            packing_summary=packing_summary,
        )
        messages.success(request, "Акт отгрузки подписан логистом.")
        return redirect(f"{reverse('shipping:dispatch-act', args=[order.pk])}?signed=logistician")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(f"{reverse('shipping:dispatch-act', args=[order.pk])}?error=logistician")


def handle_shipping_dispatch_sign_manager_request(*, request, pk: int):
    from . import dispatch as shipping_dispatch
    from . import views as shipping_views

    if request.method != "POST":
        return HttpResponseForbidden("Доступ запрещен")
    scope, role, client_agency = shipping_views._request_scope(request)
    if scope != "staff" or not shipping_views._is_manager_role(role):
        return HttpResponseForbidden("Доступ запрещен")
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "created_by", "marketplace").prefetch_related("items", "reserves"),
        pk=pk,
    )
    if not shipping_views._can_access_order(scope, client_agency, order):
        return HttpResponseForbidden("Доступ запрещен")
    packing_summary = shipping_views._shipping_packing_summary(order)
    dispatch_stage = shipping_dispatch.shipping_dispatch_stage(order, packing_summary=packing_summary)
    try:
        shipping_dispatch.sign_dispatch_act_manager(
            order,
            request.user,
            dispatch_stage=dispatch_stage,
            packing_summary=packing_summary,
        )
        messages.success(request, "Акт отгрузки подписан менеджером и отправлен клиенту.")
        return redirect(f"{reverse('shipping:dispatch-act', args=[order.pk])}?signed=manager")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(f"{reverse('shipping:dispatch-act', args=[order.pk])}?error=manager")


def download_shipping_attachment(*, request, pk: int, attachment_id: int):
    from . import views as shipping_views
    from .models import ShippingOrderAttachment

    scope, role, client_agency = shipping_views._request_scope(request)
    order = get_object_or_404(ShippingOrder.objects.select_related("agency"), pk=pk)
    if not shipping_views._can_access_order(scope, client_agency, order):
        return HttpResponseForbidden("Доступ запрещен")
    attachment = get_object_or_404(
        ShippingOrderAttachment.objects.select_related("order"),
        pk=attachment_id,
        order=order,
    )
    if attachment.is_expired or not attachment.file:
        raise Http404("Файл недоступен")
    if request.method == "GET" and str(request.GET.get("print_status") or "").strip() in {"1", "true"}:
        if scope != "staff":
            return HttpResponseForbidden("Доступ запрещен")
        from processing_app.models import ProcessingPrintJob

        jobs = ProcessingPrintJob.objects.filter(
            order_id=order.number,
            card_id=f"shipping-document:{attachment.pk}",
        )
        printed_job = jobs.filter(status=ProcessingPrintJob.STATUS_PRINTED).order_by("-updated_at", "-id").first()
        latest_job = jobs.order_by("-created_at", "-id").first()
        effective_job = printed_job or latest_job
        return JsonResponse(
            {
                "ok": True,
                "printed": bool(printed_job),
                "status": ProcessingPrintJob.STATUS_PRINTED if printed_job else str(getattr(latest_job, "status", "") or ""),
                "printed_at": printed_job.updated_at.isoformat() if printed_job and printed_job.updated_at else "",
                "job_id": int(effective_job.id) if effective_job else None,
            }
        )
    if request.method == "POST":
        if scope != "staff":
            return HttpResponseForbidden("Доступ запрещен")
        return _print_shipping_pdf_attachment(
            request=request,
            order=order,
            attachment=attachment,
        )
    inline = str(request.GET.get("inline") or "").strip().lower() in {"1", "true", "yes"}
    response = FileResponse(
        attachment.file.open("rb"),
        as_attachment=not inline,
        filename=attachment.filename,
    )
    if inline:
        response["X-Frame-Options"] = "SAMEORIGIN"
    return response


def _print_shipping_pdf_attachment(*, request, order: ShippingOrder, attachment):
    if not attachment.filename.lower().endswith(".pdf"):
        return JsonResponse(
            {"ok": False, "error": "Прямая печать доступна только для PDF-файлов."},
            status=400,
        )
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Некорректные параметры печати."}, status=400)
    if payload.get("browser_printed") is True:
        from processing_app.models import ProcessingPrintJob

        job = ProcessingPrintJob.objects.create(
            status=ProcessingPrintJob.STATUS_PRINTED,
            order_id=order.number,
            card_id=f"shipping-document:{attachment.pk}",
            article="shipping_pdf_document",
            barcode=attachment.filename[:128],
            printer_name="Печать через приложение",
            copies_count=1,
            processing_param_key="shipping_pdf_document",
            requested_by=(request.user.get_username() if request.user.is_authenticated else ""),
        )
        log_order_action(
            action="update",
            order_id=order.number,
            order_type="shipping",
            user=request.user if request.user.is_authenticated else None,
            agency=order.agency,
            description="Документ распечатан через приложение",
            payload=order_payload(
                order,
                extra={
                    "printed_attachment": attachment.filename,
                    "printer_name": "Печать через приложение",
                    "print_job_id": job.pk,
                },
            ),
        )
        return JsonResponse(
            {
                "ok": True,
                "printed": True,
                "status": ProcessingPrintJob.STATUS_PRINTED,
                "job_id": job.pk,
                "filename": attachment.filename,
                "page_count": 0,
                "printer_name": "Печать через приложение",
            }
        )
    printer_name = str(payload.get("printer_name") or "").strip()
    agent_id = str(payload.get("agent_id") or "").strip()
    if not printer_name:
        return JsonResponse({"ok": False, "error": "Выберите принтер ШК ГМ."}, status=400)
    if not agent_id:
        return JsonResponse(
            {"ok": False, "error": "Локальный агент печати не найден. Откройте настройки принтера."},
            status=409,
        )
    try:
        import fitz

        with attachment.file.open("rb") as source:
            pdf_bytes = source.read(32 * 1024 * 1024 + 1)
        if len(pdf_bytes) > 32 * 1024 * 1024:
            raise ValueError("PDF больше 32 МБ; отправьте его на печать отдельными файлами.")
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            page_count = int(document.page_count or 0)
            if page_count < 1:
                raise ValueError("В PDF нет страниц для печати.")
            if page_count > 100:
                raise ValueError("В PDF больше 100 страниц; разделите файл перед печатью.")
            images: list[str] = []
            page_dimensions: list[tuple[int, int]] = []
            payload_size = 0
            for page in document:
                rect = page.rect
                page_dimensions.append(
                    (
                        max(1, round(float(rect.width) / 72 * 25.4)),
                        max(1, round(float(rect.height) / 72 * 25.4)),
                    )
                )
                encoded = base64.b64encode(
                    page.get_pixmap(dpi=203, colorspace=fitz.csGRAY, alpha=False).tobytes("png")
                ).decode("ascii")
                images.append(encoded)
                payload_size += len(encoded)
                if payload_size > 48 * 1024 * 1024:
                    raise ValueError("PDF слишком большой для очереди печати; разделите файл.")
            width_mm, height_mm = page_dimensions[0]
            if any((width, height) != (width_mm, height_mm) for width, height in page_dimensions[1:]):
                raise ValueError("В PDF страницы разного размера; разделите файл перед печатью.")
        finally:
            document.close()
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        logger.exception(
            "Failed to prepare shipping PDF attachment for print",
            extra={"attachment_id": attachment.id},
        )
        return JsonResponse(
            {"ok": False, "error": "Не удалось подготовить PDF к печати. Проверьте файл."},
            status=400,
        )

    from processing_app.services import ProcessingWorkflowService

    result = ProcessingWorkflowService.enqueue_processing_print_job(
        request=request,
        data={
            "order_id": order.number,
            "card_id": f"shipping-document:{attachment.pk}",
            "article": "shipping_pdf_document",
            "barcode": attachment.filename[:128],
            "size": f"{width_mm}x{height_mm}",
            "printer_name": printer_name,
            "agent_id": agent_id,
            "label_png_base64_list": images,
            "processing_param_key": "shipping_pdf_document",
            "label_width_mm": width_mm,
            "label_height_mm": height_mm,
        },
    )
    if result.http_status >= 400:
        return JsonResponse(result.payload, status=result.http_status)
    log_order_action(
        action="update",
        order_id=order.number,
        order_type="shipping",
        user=request.user if request.user.is_authenticated else None,
        agency=order.agency,
        description="Документ отправлен на печать",
        payload=order_payload(
            order,
            extra={
                "printed_attachment": attachment.filename,
                "printed_pages": page_count,
                "printer_name": printer_name,
                "print_job_id": result.payload.get("job_id"),
            },
        ),
    )
    return JsonResponse(
        {
            **result.payload,
            "filename": attachment.filename,
            "page_count": page_count,
            "printer_name": printer_name,
        },
        status=result.http_status,
    )


def handle_shipping_documents_request(*, request, pk: int):
    from . import transport_note as shipping_transport_note
    from . import views as shipping_views

    scope, role, client_agency = shipping_views._request_scope(request)
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "created_by", "marketplace").prefetch_related("items"),
        pk=pk,
    )
    if not shipping_views._can_access_order(scope, client_agency, order):
        return HttpResponseForbidden("Доступ запрещен")
    if not shipping_transport_note.can_access_transport_note(order, scope, role):
        return HttpResponseForbidden("Доступ запрещен")
    note = shipping_transport_note.get_or_create_transport_note(order)
    form = shipping_views.ShippingTransportNoteForm(request.POST or None, instance=note)
    if request.method == "POST":
        if form.is_valid():
            note = form.save()
            log_order_action(
                action="update",
                order_id=order.number,
                order_type="shipping",
                user=request.user if request.user.is_authenticated else None,
                agency=order.agency,
                description="Обновлена транспортная накладная",
                payload=order_payload(
                    order,
                    extra={
                        "act": "shipping_transport_note",
                        "transport_note_id": note.pk,
                        "transport_note_number": note.document_number,
                    },
                ),
            )
            messages.success(request, "Транспортная накладная сохранена.")
            return redirect("shipping:documents", pk=order.pk)
        messages.error(request, "Проверьте поля транспортной накладной.")
    order.display_number = shipping_views._display_shipping_number(order.number)
    context = {
        "order": order,
        "form": form,
        "note": form.instance,
        "cabinet_url": resolve_cabinet_url(role),
        "scope": scope,
        "role": role,
        "selected_client": order.agency,
        "detail_url": reverse("shipping:detail", args=[order.pk]),
        "preview": shipping_transport_note.build_transport_note_preview_context(order, form.instance),
        "pdf_url": reverse("shipping:transport-note-pdf", args=[order.pk]),
        "docx_url": reverse("shipping:transport-note-docx", args=[order.pk]),
        "docx_open_url": f'{reverse("shipping:transport-note-docx", args=[order.pk])}?inline=1',
        "return_act_url": reverse("shipping:return-act-doc", args=[order.pk]),
        "return_act_open_url": f'{reverse("shipping:return-act-doc", args=[order.pk])}?inline=1',
    }
    return render(request, "shipping/transport_note.html", context)


def build_shipping_transport_note_pdf_response(*, request, pk: int):
    from . import transport_note as shipping_transport_note
    from . import views as shipping_views

    scope, role, client_agency = shipping_views._request_scope(request)
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "created_by", "marketplace").prefetch_related("items"),
        pk=pk,
    )
    if not shipping_views._can_access_order(scope, client_agency, order):
        return HttpResponseForbidden("Доступ запрещен")
    if not shipping_transport_note.can_access_transport_note(order, scope, role):
        return HttpResponseForbidden("Доступ запрещен")
    note = shipping_transport_note.get_or_create_transport_note(order)
    pdf_bytes = shipping_transport_note.render_transport_note_pdf(order, note)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{shipping_transport_note.transport_note_filename(order, note)}"'
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


def build_shipping_transport_note_docx_response(*, request, pk: int):
    from . import transport_note as shipping_transport_note
    from . import views as shipping_views

    scope, role, client_agency = shipping_views._request_scope(request)
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "created_by", "marketplace").prefetch_related("items"),
        pk=pk,
    )
    if not shipping_views._can_access_order(scope, client_agency, order):
        return HttpResponseForbidden("Доступ запрещен")
    if not shipping_transport_note.can_access_transport_note(order, scope, role):
        return HttpResponseForbidden("Доступ запрещен")
    note = shipping_transport_note.get_or_create_transport_note(order)
    docx_bytes = shipping_transport_note.render_transport_note_docx(order, note)
    response = HttpResponse(
        docx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    disposition = "inline" if request.GET.get("inline") in {"1", "true", "yes"} else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="{shipping_transport_note.transport_note_docx_filename(order, note)}"'
    return response


def build_shipping_return_act_response(*, request, pk: int):
    from . import return_act as shipping_return_act
    from . import transport_note as shipping_transport_note
    from . import views as shipping_views

    scope, role, client_agency = shipping_views._request_scope(request)
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "created_by", "marketplace").prefetch_related("items"),
        pk=pk,
    )
    if not shipping_views._can_access_order(scope, client_agency, order):
        return HttpResponseForbidden("Доступ запрещен")
    if not shipping_transport_note.can_access_transport_note(order, scope, role):
        return HttpResponseForbidden("Доступ запрещен")
    doc_bytes = shipping_return_act.render_return_act_doc(order)
    response = HttpResponse(
        doc_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    disposition = "inline" if request.GET.get("inline") in {"1", "true", "yes"} else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="{shipping_return_act.return_act_doc_filename(order)}"'
    return response


def handle_shipping_packing_request(*, request, pk: int):
    from . import selectors as shipping_selectors
    from . import dispatch as shipping_dispatch
    from . import views as shipping_views

    scope, role, client_agency = shipping_views._request_scope(request)
    if scope != "staff" or not shipping_views._is_storekeeper_role(role):
        return HttpResponseForbidden("Доступ запрещен")
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "marketplace"),
        pk=pk,
    )
    if not shipping_views._can_storekeeper_manage_packing(scope, role, order):
        return HttpResponseForbidden("Доступ запрещен")
    ozon_intake_errors = _strict_ozon_gm_intake_errors(order)
    if ozon_intake_errors:
        messages.error(
            request,
            "Паллетизация заблокирована: " + "; ".join(ozon_intake_errors[:4]),
        )
        return redirect("shipping:detail", pk=order.pk)
    edit_existing_packing = str(request.GET.get("edit") or request.POST.get("edit") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if order.status == ShippingOrder.STATUS_PACKED:
        if not edit_existing_packing and request.method == "GET":
            return redirect("shipping:packing-slips", pk=order.pk)
        if not edit_existing_packing:
            return HttpResponseForbidden("Доступ запрещен")
        dispatch_stage = shipping_dispatch.shipping_dispatch_stage(order)
        if dispatch_stage.get("has_trip") or dispatch_stage.get("entry"):
            messages.error(request, "Нельзя изменить раскладку: уже создан рейс или акт отгрузки.")
            return redirect("shipping:detail", pk=order.pk)
    from .discrepancy import resolve_completed_discrepancy_supplement

    if resolve_completed_discrepancy_supplement(order, user=request.user):
        order.refresh_from_db()
    loose_state = shipping_views._shipping_loose_packing_initial_state(order)
    if loose_state["loose_items"]:
        if request.method == "POST":
            messages.error(request, "Сначала упакуйте товар без короба в короба, затем продолжите паллетизацию.")
        return redirect("shipping:packing-loose", pk=order.pk)
    ozon_gm_repack = _ozon_gm_repack_context(order)
    if ozon_gm_repack.get("required"):
        if not ozon_gm_repack.get("ready"):
            for error in ozon_gm_repack.get("errors") or []:
                messages.error(request, error)
            return redirect("shipping:detail", pk=order.pk)
        return redirect("shipping:packing-loose", pk=order.pk)
    packing_state = shipping_views._shipping_packing_initial_state(order)
    if request.method == "GET" and packing_state["packing_summary"] and not edit_existing_packing:
        return redirect("shipping:packing-slips", pk=order.pk)
    if not packing_state["delivered_boxes"]:
        messages.error(request, "Нет доставленных в OTG коробов для раскладки по паллетам.")
        return redirect("shipping:detail", pk=order.pk)
    pending_removal = shipping_views.shipping_packing_pending_pallet_removal(order) or {}
    if request.method == "POST":
        try:
            boxes_state = json.loads(request.POST.get("boxes_json") or "[]")
            pallets_state = json.loads(request.POST.get("pallets_json") or "[]")
        except json.JSONDecodeError:
            messages.error(request, "Не удалось прочитать раскладку коробов по паллетам.")
        else:
            try:
                result = shipping_views.save_shipping_packing(
                    order,
                    boxes_state=boxes_state,
                    pallets_state=pallets_state,
                    delivered_boxes=packing_state["delivered_boxes"],
                    initial_boxes=packing_state["initial_boxes"],
                    user=request.user,
                    replace_existing=edit_existing_packing,
                )
            except (ValueError, ValidationError) as exc:
                result = {"saved": False, "errors": [str(exc)]}
            if result["saved"]:
                messages.success(request, "Раскладка коробов по новым паллетам сохранена.")
                return redirect("shipping:packing-slips", pk=order.pk)
            for error in result["errors"]:
                messages.error(request, error)
    storekeeper_binding_alert = _storekeeper_marketplace_binding_alert(
        order,
        boxes=packing_state["delivered_boxes"],
    )
    from .discrepancy import shipping_discrepancy_context

    packing_discrepancy = shipping_discrepancy_context(order)
    if (
        packing_discrepancy.get("is_v2")
        and packing_discrepancy.get("status")
        in {"pending", "pick_confirmation", "pickup_required"}
        and storekeeper_binding_alert.get("show")
    ):
        storekeeper_binding_alert["blocking"] = True
    order.display_number = shipping_views._display_shipping_number(order.number)
    context = {
        "order": order,
        "box_rows": packing_state["initial_boxes"],
        "boxes_data": packing_state["initial_boxes"],
        "pallets_data": packing_state["initial_pallets"],
        "packing_summary": packing_state["packing_summary"],
        "cabinet_url": resolve_cabinet_url(role),
        "scope": scope,
        "role": role,
        "selected_client": order.agency,
        "detail_url": f"/shipping/{order.pk}/",
        "client_label": order.agency.agn_name or "-",
        "status_label": shipping_selectors.shipping_ui_status_label(order),
        "packing_pallet_removal_pending": pending_removal,
        "pallet_removal_request_url": f"/shipping/{order.pk}/packing/pallet-removal/request/",
        "packing_slip_meta": shipping_views.shipping_packing_slip_meta(order),
        "packing_edit_mode": edit_existing_packing,
        "storekeeper_binding_alert": storekeeper_binding_alert,
        "packing_discrepancy": packing_discrepancy,
    }
    context.update(_shipping_print_agent_context(request=request, order_pk=order.pk))
    return render(request, "fbs/shipping_packing.html", context)


def handle_shipping_packing_pallet_removal_request(*, request, pk: int):
    from django.http import JsonResponse

    from . import views as shipping_views

    scope, role, _client_agency = shipping_views._request_scope(request)
    if scope != "staff" or not shipping_views._is_storekeeper_role(role):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "marketplace"),
        pk=pk,
    )
    if not shipping_views._can_storekeeper_manage_packing(scope, role, order):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (TypeError, ValueError, UnicodeDecodeError):
        payload = {}
    result = shipping_views.request_shipping_packing_pallet_removal(
        order,
        pallet_code=payload.get("pallet_code"),
        reason=payload.get("reason"),
        boxes_state=payload.get("boxes") or [],
        pallets_state=payload.get("pallets") or [],
        user=request.user,
    )
    return JsonResponse(result, status=200 if result.get("ok") else 400)


def handle_shipping_packing_pallet_removal_review_request(*, request, pk: int):
    from . import views as shipping_views

    scope, role, _client_agency = shipping_views._request_scope(request)
    if scope != "staff" or role not in {"manager", "head_manager", "director", "admin"}:
        return HttpResponseForbidden("Доступ запрещен")
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "marketplace"),
        pk=pk,
    )
    if request.method == "POST":
        decision = (request.POST.get("decision") or "").strip()
        result = shipping_views.resolve_shipping_packing_pallet_removal(
            order,
            decision=decision,
            user=request.user,
        )
        if result.get("ok"):
            if result.get("payload", {}).get("removal_status") == "approved":
                messages.success(request, "\u041f\u0430\u043b\u043b\u0435\u0442\u0430 \u0438\u0437\u044a\u044f\u0442\u0430 \u0438\u0437 \u0437\u0430\u044f\u0432\u043a\u0438.")
            else:
                messages.success(request, "\u0418\u0437\u044a\u044f\u0442\u0438\u0435 \u043f\u0430\u043b\u043b\u0435\u0442\u044b \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u043e.")
        else:
            messages.error(request, result.get("error") or "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u0430\u0442\u044c \u0440\u0435\u0448\u0435\u043d\u0438\u0435.")
        return redirect("shipping:packing-pallet-removal-review", pk=order.pk)

    pending_removal = shipping_views.shipping_packing_pending_pallet_removal(order) or {}
    order.display_number = shipping_views._display_shipping_number(order.number)
    return render(
        request,
        "shipping/pallet_removal_review.html",
        {
            "order": order,
            "pending_removal": pending_removal,
            "detail_url": f"/shipping/{order.pk}/",
            "packing_url": f"/shipping/{order.pk}/packing/",
        },
    )


def handle_shipping_loose_packing_request(*, request, pk: int):
    from . import selectors as shipping_selectors
    from . import views as shipping_views

    scope, role, client_agency = shipping_views._request_scope(request)
    if scope != "staff" or not shipping_views._is_storekeeper_role(role):
        return HttpResponseForbidden("Доступ запрещен")
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "marketplace"),
        pk=pk,
    )
    if not shipping_views._can_storekeeper_manage_packing(scope, role, order):
        return HttpResponseForbidden("Доступ запрещен")
    loose_draft_session_key = f"shipping_loose_packing_draft:{int(order.pk)}"
    saved_loose_draft = request.session.get(loose_draft_session_key)
    if not isinstance(saved_loose_draft, dict):
        saved_loose_draft = {}

    ozon_intake_errors = _strict_ozon_gm_intake_errors(order)
    if ozon_intake_errors:
        messages.error(
            request,
            "Упаковка заблокирована: " + "; ".join(ozon_intake_errors[:4]),
        )
        return redirect("shipping:detail", pk=order.pk)

    loose_state = shipping_views._shipping_loose_packing_initial_state(order)
    _refresh_ozon_gm_composition_for_loose_packing(order, user=request.user)
    ozon_gm_repack = _ozon_gm_repack_context(order)
    repack_existing_boxes = bool(
        ozon_gm_repack.get("required") and ozon_gm_repack.get("ready")
    )
    if ozon_gm_repack.get("required") and not ozon_gm_repack.get("ready"):
        for error in ozon_gm_repack.get("errors") or []:
            messages.error(request, error)
        return redirect("shipping:detail", pk=order.pk)
    if repack_existing_boxes:
        loose_state = shipping_views._shipping_loose_packing_initial_state(
            order,
            repack_existing_boxes=True,
        )
        gm_plan = ozon_gm_repack["gm_plan"]
    else:
        gm_plan = _ozon_loose_packing_gm_plan(order, loose_items=loose_state["loose_items"])
    submitted_boxes_state = []
    submitted_pallets_state = []
    if gm_plan["required"] and not gm_plan["ready"] and gm_plan["errors"]:
        for error in gm_plan["errors"][:4]:
            messages.error(request, "Упаковка пока недоступна: " + str(error))
        return redirect("shipping:detail", pk=order.pk)
    if not loose_state["loose_items"]:
        return redirect("shipping:packing", pk=order.pk)
    if request.method == "POST":
        if gm_plan["required"] and not gm_plan["ready"]:
            for error in gm_plan["errors"]:
                messages.error(request, error)
        else:
            try:
                submitted_boxes_state = json.loads(request.POST.get("boxes_json") or "[]")
                submitted_pallets_state = json.loads(request.POST.get("pallets_json") or "[]")
                submitted_repack_source_boxes = json.loads(
                    request.POST.get("repack_source_boxes_json") or "[]"
                )
            except json.JSONDecodeError:
                messages.error(request, "Не удалось прочитать упаковку товара без короба.")
            else:
                if not isinstance(submitted_repack_source_boxes, list):
                    submitted_repack_source_boxes = []
                try:
                    result = shipping_views.save_shipping_loose_packing(
                        order,
                        boxes_state=submitted_boxes_state,
                        pallets_state=submitted_pallets_state,
                        loose_items=loose_state["loose_items"],
                        gm_targets=gm_plan["targets"] if gm_plan["required"] else None,
                        repack_existing_boxes=repack_existing_boxes,
                        repack_source_box_codes=submitted_repack_source_boxes,
                        user=request.user,
                    )
                except (ValueError, ValidationError) as exc:
                    result = {"saved": False, "errors": [str(exc)]}
                if result["saved"]:
                    request.session.pop(loose_draft_session_key, None)
                    request.session.modified = True
                    from .discrepancy import close_loose_packing_task

                    close_loose_packing_task(order)
                    supplement_resolved = False
                    discrepancy_payload = (
                        order.shipping_discrepancy_payload
                        if isinstance(order.shipping_discrepancy_payload, dict)
                        else {}
                    )
                    if discrepancy_payload.get("workflow_stage") == "awaiting_supplement_packing":
                        try:
                            from .discrepancy import resolve_completed_discrepancy_supplement

                            resolved_payload = resolve_completed_discrepancy_supplement(
                                order,
                                user=request.user,
                            )
                            supplement_resolved = resolved_payload.get("status") == "resolved"
                        except Exception:
                            logger.exception(
                                "Loose packing was saved, but supplement discrepancy resolution failed for shipping order %s",
                                order.pk,
                            )
                            messages.warning(
                                request,
                                "Товар упакован, но статус добора не закрылся автоматически. "
                                "Не упаковывайте его повторно; сообщите менеджеру.",
                            )
                    if result.get("completed"):
                        messages.success(request, "Товар без короба упакован в короба. Паллетизация завершена.")
                        return redirect("shipping:packing-slips", pk=order.pk)
                    if repack_existing_boxes:
                        messages.success(
                            request,
                            "Исходные короба переупакованы по ШК ГМ Ozon. Теперь разложите грузоместа по паллетам.",
                        )
                        return redirect("shipping:packing", pk=order.pk)
                    if supplement_resolved:
                        messages.success(
                            request,
                            "Добор упакован в короба, расхождение закрыто. Можно продолжить паллетизацию.",
                        )
                    else:
                        messages.success(request, "Товар без короба упакован в короба. Можно продолжить паллетизацию.")
                    return redirect("shipping:packing", pk=order.pk)
                for error in result["errors"]:
                    messages.error(request, error)

    order.display_number = shipping_views._display_shipping_number(order.number)
    context = {
        "order": order,
        "loose_items": loose_state["loose_items"],
        "existing_box_codes": loose_state["existing_box_codes"],
        "box_code_base": loose_state["box_code_base"],
        "gm_plan": gm_plan,
        "repack_existing_boxes": repack_existing_boxes,
        "repack_source_box_codes_data": loose_state.get("source_box_codes") or [],
        "container_code_url": f"/shipping/{order.pk}/packing/loose/container-code/",
        "marking_scan_url": f"/shipping/{order.pk}/packing/loose/marking-scan/",
        "packing_draft_url": f"/shipping/{order.pk}/packing/loose/draft/",
        "packing_draft": saved_loose_draft,
        "requires_marking_scan": bool(
            any(bool(item.get("requires_marking_scan")) for item in loose_state["loose_items"])
        ),
        "boxes_data": submitted_boxes_state,
        "pallets_data": submitted_pallets_state,
        "cabinet_url": resolve_cabinet_url(role),
        "scope": scope,
        "role": role,
        "selected_client": order.agency,
        "detail_url": f"/shipping/{order.pk}/",
        "packing_url": f"/shipping/{order.pk}/packing/",
        "client_label": order.agency.agn_name or "-",
        "status_label": shipping_selectors.shipping_ui_status_label(order),
    }
    return render(request, "shipping/loose_packing.html", context)


def handle_shipping_packing_slips_request(*, request, pk: int):
    from . import selectors as shipping_selectors
    from . import views as shipping_views

    scope, role, client_agency = shipping_views._request_scope(request)
    if scope != "staff" or not shipping_views._is_storekeeper_role(role):
        return HttpResponseForbidden("Доступ запрещен")
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "marketplace"),
        pk=pk,
    )
    packing_summary = shipping_views._shipping_packing_summary(order)
    if not packing_summary:
        messages.error(request, "Упаковочные листы доступны только после завершения паллетизации.")
        return redirect("shipping:packing", pk=order.pk)
    marketplace_name = str(getattr(order.marketplace, "name", "") or "").strip()
    is_ozon = "ozon" in marketplace_name.casefold()
    packing_slips_data = shipping_views.shipping_packing_slips_data(order, packing_summary)
    ozon_gm_labels_document = {}
    packing_slips_error = ""
    if is_ozon:
        packing_slips_data = []
        ozon_summary = _ozon_api_summary_for_form(order)
        gm_cargoes = list(ozon_summary.get("gm_cargoes") or [])
        expected_gm_barcodes = list(ozon_summary.get("gm_barcodes") or [])
        if not gm_cargoes:
            packing_slips_error = "В заявке нет сохранённых грузомест Ozon для печати этикеток ШК ГМ."
        else:
            from .ozon_label_api import fetch_ozon_gm_label_documents
            from .ozon_labels import build_ozon_gm_label_assets

            documents, label_error, pending = fetch_ozon_gm_label_documents(
                order.agency,
                gm_cargoes,
                expected_gm_barcodes=expected_gm_barcodes,
            )
            if pending or label_error:
                packing_slips_error = label_error or "Ozon ещё формирует этикетки. Обновите страницу через несколько секунд."
            elif documents:
                try:
                    ozon_gm_labels_document = build_ozon_gm_label_assets(documents, gm_cargoes)
                except ValueError as exc:
                    packing_slips_error = str(exc)
                else:
                    supply_number = str(ozon_summary.get("supply_number") or order.wb_supply_barcode or "").strip()
                    packing_slips_data = list(ozon_gm_labels_document.get("labels") or [])
                    for label in packing_slips_data:
                        label["supply_number"] = supply_number or str(label.get("supply_id") or "-")
    print_status_payload = shipping_views.build_print_status_snapshot()
    order.display_number = shipping_views._display_shipping_number(order.number)
    context = {
        "order": order,
        "packing_summary": packing_summary,
        "packing_slips_data": packing_slips_data,
        "packing_slip_kind": "ozon_gm" if is_ozon else "standard",
        "packing_slips_error": packing_slips_error,
        "ozon_gm_labels_document": ozon_gm_labels_document,
        "cabinet_url": resolve_cabinet_url(role),
        "scope": scope,
        "role": role,
        "selected_client": order.agency,
        "detail_url": f"/shipping/{order.pk}/",
        "packing_url": f"/shipping/{order.pk}/packing/",
        "client_label": order.agency.agn_name or "-",
        "status_label": shipping_selectors.shipping_ui_status_label(order),
        "print_status_payload": print_status_payload,
    }
    context.update(_shipping_print_agent_context(request=request, order_pk=order.pk))
    context.update(print_status_payload)
    return render(request, "shipping/packing_slips.html", context)


def handle_shipping_packing_slips_status_request(*, request, pk: int):
    from . import views as shipping_views

    scope, role, client_agency = shipping_views._request_scope(request)
    if scope != "staff" or not shipping_views._is_storekeeper_role(role):
        return HttpResponseForbidden("Доступ запрещен")
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "marketplace"),
        pk=pk,
    )
    if not shipping_views._shipping_packing_summary(order):
        return JsonResponse({"ok": False, "error": "packing_not_ready"}, status=400)
    refresh_message = ""
    if request.method == "POST":
        payload, refresh_message = shipping_views.refresh_print_agent_printers()
    else:
        payload = shipping_views.build_print_status_snapshot()
    return JsonResponse({"ok": True, **payload, "refresh_message": refresh_message})


def _warehouse_trip_number_for_shipping(order: ShippingOrder) -> str:
    from .dispatch import shipping_dispatch_trip_link

    trip_link = shipping_dispatch_trip_link(order)
    trip = trip_link.trip if trip_link else None
    return str(getattr(trip, "number", "") or "").strip()


def _warehouse_shipping_snapshots_for_order(
    order: ShippingOrder,
    *,
    state_codes: list[str] | tuple[str, ...] | None = None,
    lock: bool = False,
) -> list[WarehouseStockSnapshot]:
    qs = WarehouseStockSnapshot.objects.select_related("active_operation", "last_event").filter(
        agency=order.agency,
        is_archived=False,
        last_event__stock_context_type="shipping",
        last_event__stock_context_id=order.number,
    )
    if lock:
        qs = qs.select_for_update(of=("self",))
    if state_codes:
        qs = qs.filter(warehouse_state_code__in=[str(code or "").strip() for code in state_codes if str(code or "").strip()])
    return list(qs.order_by("id"))


def _ensure_warehouse_loaded_for_shipping(
    order: ShippingOrder,
    *,
    trip_number: str,
    user=None,
) -> bool:
    snapshots = _warehouse_shipping_snapshots_for_order(
        order,
        state_codes=(
            WarehouseStateCode.RESERVED_FOR_SHIPPING.value,
            WarehouseStateCode.MOVING_TO_OTG.value,
            WarehouseStateCode.IN_OTG.value,
            WarehouseStateCode.PALLETIZING.value,
            WarehouseStateCode.READY_FOR_LOADING.value,
            WarehouseStateCode.ASSIGNED_TO_TRIP.value,
            WarehouseStateCode.LOADING_IN_PROGRESS.value,
            WarehouseStateCode.LOADED_TO_VEHICLE.value,
        ),
    )
    if not snapshots:
        return False

    try:
        WarehouseWritePathService.validate_shipping_pallet_ownership(
            agency=order.agency,
            order_id=order.number,
            snapshots=snapshots,
            require_pallets=True,
        )
    except ValueError:
        return False

    return all(
        str(snapshot.warehouse_state_code or "").strip()
        == WarehouseStateCode.LOADED_TO_VEHICLE.value
        and str(snapshot.current_trip_id or "").strip() == trip_number
        and bool(snapshot.is_in_vehicle)
        for snapshot in snapshots
    )


def shipping_loaded_qty_by_item(
    order: ShippingOrder,
    *,
    trip_number: str | None = None,
) -> dict[int, int]:
    """Return confirmed loading fact allocated strictly by product barcode."""
    trip_number = str(trip_number or _warehouse_trip_number_for_shipping(order) or "").strip()
    if not trip_number:
        raise ValidationError("Для отгрузки не найден подтвержденный рейс.")
    if not _ensure_warehouse_loaded_for_shipping(order, trip_number=trip_number):
        raise ValidationError(
            "Факт погрузки не подтвержден: не все складские строки заявки загружены в этот рейс."
        )

    loaded_by_barcode: Counter[str] = Counter()
    for snapshot in _warehouse_shipping_snapshots_for_order(
        order,
        state_codes=[WarehouseStateCode.LOADED_TO_VEHICLE.value],
    ):
        if (
            str(snapshot.current_trip_id or "").strip() != trip_number
            or not snapshot.is_in_vehicle
        ):
            continue
        qty = max(int(snapshot.qty or 0), 0)
        if qty == 0:
            continue
        barcode = str(snapshot.barcode or "").strip().casefold()
        if not barcode:
            raise ValidationError(
                f"Короб {snapshot.container_code or snapshot.pk}: отсутствует ШК товара. "
                "Отгрузка без подтвержденного ШК запрещена."
            )
        loaded_by_barcode[barcode] += qty

    if not loaded_by_barcode:
        raise ValidationError("В рейсе нет подтвержденного количества товара по ШК.")

    items = list(order.items.order_by("id"))
    requested_by_barcode: Counter[str] = Counter()
    for item in items:
        requested_qty = max(int(item.qty_requested or 0), 0)
        if requested_qty == 0:
            continue
        barcode = str(item.barcode or "").strip().casefold()
        if not barcode:
            raise ValidationError(
                f"Позиция {item.sku_code or item.pk}: в заявке отсутствует ШК товара."
            )
        requested_by_barcode[barcode] += requested_qty

    extra_barcodes = sorted(set(loaded_by_barcode) - set(requested_by_barcode))
    if extra_barcodes:
        raise ValidationError(
            "В рейсе обнаружен товар, которого нет в заявке: ШК "
            + ", ".join(extra_barcodes)
            + "."
        )
    excessive_barcodes = [
        barcode
        for barcode, loaded_qty in loaded_by_barcode.items()
        if loaded_qty > int(requested_by_barcode.get(barcode, 0) or 0)
    ]
    if excessive_barcodes:
        raise ValidationError(
            "Факт погрузки превышает заявленное количество по ШК: "
            + ", ".join(sorted(excessive_barcodes))
            + "."
        )

    remaining = Counter(loaded_by_barcode)
    result: dict[int, int] = {}
    for item in items:
        barcode = str(item.barcode or "").strip().casefold()
        take_qty = min(
            max(int(item.qty_requested or 0), 0),
            max(int(remaining.get(barcode, 0)), 0),
        )
        result[item.id] = take_qty
        remaining[barcode] -= take_qty
    if any(int(qty or 0) > 0 for qty in remaining.values()):
        raise ValidationError("Не удалось однозначно распределить факт погрузки по позициям заявки.")
    return result


def _can_ship_via_warehouse_write_path(
    order: ShippingOrder,
    *,
    shipped_qty_by_item: dict[int, int],
    user=None,
) -> str:
    trip_number = _warehouse_trip_number_for_shipping(order)
    if not trip_number:
        return ""
    loaded_qty_by_item = shipping_loaded_qty_by_item(
        order,
        trip_number=trip_number,
    )
    normalized_fact = {
        int(item_id): max(int(qty or 0), 0)
        for item_id, qty in shipped_qty_by_item.items()
    }
    if normalized_fact != loaded_qty_by_item:
        raise ValidationError(
            "Переданное количество отгрузки не совпадает с подтвержденными сканами погрузки по ШК."
        )
    return trip_number


def _ship_reserved_order_without_trip(order: ShippingOrder, user=None) -> None:
    try:
        WarehouseWritePathService.ship_order_without_trip(
            agency=order.agency,
            order_id=order.number,
            performed_by=user,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


@transaction.atomic
def ship_order(order: ShippingOrder, user=None, *, shipped_qty_by_item: dict[int, int] | None = None) -> None:
    locked_order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    for field in ShippingOrder._meta.concrete_fields:
        setattr(order, field.attname, getattr(locked_order, field.attname))
    if order.is_closed():
        raise ValidationError("Нельзя отгрузить закрытую заявку.")
    items = list(order.items.order_by("id"))
    if not items:
        raise ValidationError("В заявке нет позиций для отгрузки.")

    from .packing import _shipping_delivered_boxes

    delivered_boxes = _shipping_delivered_boxes(order)
    binding_errors = enforced_marketplace_item_binding_errors(
        order,
        boxes=delivered_boxes,
    )
    if binding_errors:
        raise ValidationError(binding_errors)
    binding_result = validate_marketplace_item_binding(
        order,
        boxes=delivered_boxes,
    )
    final_shipping_truth = build_shipping_final_truth_snapshot(
        order,
        result=binding_result,
    )
    if final_shipping_truth:
        final_shipping_truth["document_rows"] = shipping_final_truth_rows(
            order,
            result=binding_result,
            boxes=delivered_boxes,
        )
        final_shipping_truth["captured_at"] = timezone.localtime().isoformat()

    overrides = shipped_qty_by_item or {}
    normalized_shipped_qty: dict[int, int] = {}
    for item in items:
        raw_qty = overrides.get(item.id)
        if raw_qty is None:
            raw_qty = int(item.qty_reserved or item.qty_requested or 0)
        qty = int(raw_qty or 0)
        if qty < 0:
            raise ValidationError("Количество отгрузки не может быть отрицательным.")
        if qty > int(item.qty_requested or 0):
            raise ValidationError(
                f"{item.sku_code}/{item.size or '-'}: отгрузка больше запрошенного количества."
            )
        normalized_shipped_qty[item.id] = qty

    warehouse_trip_number = _can_ship_via_warehouse_write_path(
        order,
        shipped_qty_by_item=normalized_shipped_qty,
        user=user,
    )
    if warehouse_trip_number:
        WarehouseWritePathService.ship_order(
            agency=order.agency,
            order_id=order.number,
            trip_id=warehouse_trip_number,
            performed_by=user,
        )
    else:
        can_ship_directly = all(
            int(normalized_shipped_qty.get(item.id, 0) or 0) == max(int(item.qty_reserved or 0), 0)
            for item in items
        )
        if not can_ship_directly:
            raise ValidationError(
                "Отгрузка должна быть подтверждена через складской контур: товар должен быть загружен в рейс в центре истины."
            )
        has_order_reserves = WarehouseReserve.objects.filter(
            agency=order.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=order.number,
        ).exclude(
            status__in=[
                WarehouseReserve.STATUS_RELEASED,
                WarehouseReserve.STATUS_CANCELED,
            ]
        ).exists()
        if has_order_reserves:
            _ship_reserved_order_without_trip(order, user=user)

    shipped_items: list[dict] = []
    for item in items:
        qty = int(normalized_shipped_qty.get(item.id, 0) or 0)
        item.qty_shipped = qty
        item.qty_reserved = max(int(item.qty_reserved or 0) - qty, 0)
        item.save(update_fields=["qty_shipped", "qty_reserved", "updated_at"])
        shipped_items.append(
            {
                "sku": item.sku_code,
                "size": item.size,
                "goods_type": item.goods_type,
                "barcode": item.barcode,
                "qty": qty,
            }
        )

    WarehouseWritePathService.replace_shipping_reserves(
        agency=order.agency,
        order_id=order.number,
        items=[],
        created_by=user if getattr(user, "is_authenticated", False) else None,
        source_document_type="shipping_order",
        source_document_id=order.number,
    )
    if final_shipping_truth:
        order.status = (
            ShippingOrder.STATUS_SHIPPED
            if final_shipping_truth.get("matches_plan")
            else ShippingOrder.STATUS_PARTIAL
        )
        discrepancy_payload = (
            dict(order.shipping_discrepancy_payload)
            if isinstance(order.shipping_discrepancy_payload, dict)
            else {}
        )
        discrepancy_payload["final_shipping_truth"] = final_shipping_truth
        order.shipping_discrepancy_payload = discrepancy_payload
    else:
        order.status = (
            ShippingOrder.STATUS_SHIPPED
            if all(int(item.qty_shipped or 0) >= int(item.qty_requested or 0) for item in items)
            else ShippingOrder.STATUS_PARTIAL
        )
    order.shipped_at = timezone.now()
    order_update_fields = ["status", "shipped_at", "updated_at"]
    if final_shipping_truth:
        order_update_fields.append("shipping_discrepancy_payload")
    order.save(update_fields=order_update_fields)

    log_stock_move(
        action="update",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=order.agency,
        description=f"Отгрузка {order.number}: списание товара со склада",
        snapshot={
            "shipping_order": order.number,
            "items": shipped_items,
            "final_shipping_truth": final_shipping_truth,
        },
    )
    _log_order(
        order,
        action="status",
        user=user,
        description="Заявка отгружена",
        extra={
            "shipping_state": order.status,
            "shipped_items": shipped_items,
            "final_shipping_truth": final_shipping_truth,
        },
    )


@transaction.atomic
def complete_transfer_without_trip(order: ShippingOrder, user=None) -> bool:
    locked_order = ShippingOrder.objects.select_for_update().get(pk=order.pk)
    for field in ShippingOrder._meta.concrete_fields:
        setattr(order, field.attname, getattr(locked_order, field.attname))

    if order.delivery_type != ShippingOrder.DELIVERY_TRANSFER:
        raise ValidationError("Завершение без рейса доступно только для заявки типа «Перемещение».")
    if order.status == ShippingOrder.STATUS_SHIPPED:
        return False
    if order.is_closed():
        raise ValidationError("Нельзя завершить закрытую заявку на перемещение.")
    if order.status != ShippingOrder.STATUS_PACKED:
        raise ValidationError("Сначала завершите паллетизацию заявки на перемещение.")

    from logistics.models import LogisticsTripOrder

    if LogisticsTripOrder.objects.filter(shipping_order=order).exists():
        raise ValidationError("Заявка на перемещение уже связана с рейсом. Требуется ручной разбор.")

    snapshots = _warehouse_shipping_snapshots_for_order(
        order,
        state_codes=(
            WarehouseStateCode.MOVING_TO_OTG.value,
            WarehouseStateCode.IN_OTG.value,
            WarehouseStateCode.PALLETIZING.value,
            WarehouseStateCode.READY_FOR_LOADING.value,
            WarehouseStateCode.ASSIGNED_TO_TRIP.value,
            WarehouseStateCode.LOADING_IN_PROGRESS.value,
            WarehouseStateCode.LOADED_TO_VEHICLE.value,
        ),
        lock=True,
    )
    if not snapshots:
        raise ValidationError("Не найден складской факт паллетизации по заявке на перемещение.")
    if any(str(snapshot.current_trip_id or "").strip() or snapshot.is_in_vehicle for snapshot in snapshots):
        raise ValidationError("Товар уже связан с рейсом или погрузкой. Требуется ручной разбор.")
    if any(
        str(snapshot.warehouse_state_code or "").strip()
        != WarehouseStateCode.READY_FOR_LOADING.value
        for snapshot in snapshots
    ):
        raise ValidationError("Не все паллеты заявки подтверждены и готовы к списанию.")

    try:
        WarehouseWritePathService.validate_shipping_pallet_ownership(
            agency=order.agency,
            order_id=order.number,
            snapshots=snapshots,
            require_pallets=True,
            lock=True,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    items = list(order.items.order_by("id"))
    if not items:
        raise ValidationError("В заявке нет позиций для списания.")
    if any(int(item.qty_reserved or 0) != int(item.qty_requested or 0) for item in items):
        raise ValidationError("Количество в резерве не совпадает с заявленным количеством.")
    has_active_reserves = WarehouseReserve.objects.select_for_update().filter(
        agency=order.agency,
        reserve_type=WarehouseReserve.TYPE_SHIPPING,
        context_type="shipping",
        context_id=order.number,
    ).exclude(
        status__in=[
            WarehouseReserve.STATUS_RELEASED,
            WarehouseReserve.STATUS_CANCELED,
        ]
    ).exists()
    if not has_active_reserves:
        raise ValidationError("Не найден активный складской резерв заявки на перемещение.")

    ship_order(
        order,
        user,
        shipped_qty_by_item={item.id: int(item.qty_requested or 0) for item in items},
    )
    order.refresh_from_db(fields=["status", "shipped_at"])
    if order.status != ShippingOrder.STATUS_SHIPPED:
        raise ValidationError("Фактический состав не совпал с заявкой. Списание отменено.")

    close_storekeeper_task(order)
    close_logistician_task(order)
    close_shipping_act_manager_task(order)
    log_order_action(
        "status",
        order_id=order.number,
        order_type="shipping",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=order.agency,
        description="Перемещение завершено без рейса. Товар списан со склада",
        payload={
            "shipping_state": order.status,
            "delivery_type": ShippingOrder.DELIVERY_TRANSFER,
            "completed_without_trip": True,
        },
    )
    return True
