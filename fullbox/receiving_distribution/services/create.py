"""
Транзакционное создание:
головная приёмка (существующий ReceivingWorkflowService)
+ направления + строки + связанные черновики отгрузки.

Не вызывает reserve/pick/складскую приёмку/печать/ШК.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from audit.models import OrderAuditEntry
from fullbox.order_numbers import next_public_order_number
from orders.services import ReceivingWorkflowService
from shipping.models import ShippingOrder, ShippingOrderItem
from shipping.services import next_shipping_number, order_payload
from sku.models import Agency, Market, SKU

from ..models import (
    ReceivingDistributionAllocation,
    ReceivingDistributionDirection,
    ReceivingDistributionEvent,
    ReceivingDistributionItem,
    ReceivingDistributionPlan,
)
from .validate import DistributionDraft, validate_distribution_draft


@dataclass
class CreateResult:
    plan: ReceivingDistributionPlan
    receiving_order_id: str
    shipping_numbers: list[str]
    created: bool


def _parse_eta(raw) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip()
        dt = parse_datetime(text)
        if not dt:
            d = parse_date(text)
            if not d:
                return None
            dt = datetime.combine(d, datetime.min.time())
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _resolve_market(name: str) -> Market | None:
    text = str(name or "").strip()
    if not text:
        return None
    market = Market.objects.filter(name__iexact=text).first()
    if market:
        return market
    # Частые алиасы
    aliases = {
        "wb": "WB",
        "wildberries": "WB",
        "ozon": "Ozon",
        "яндекс": "Яндекс Маркет",
        "yandex": "Яндекс Маркет",
    }
    mapped = aliases.get(text.casefold())
    if mapped:
        return Market.objects.filter(name__iexact=mapped).first()
    return None


def _client_sku_codes(agency: Agency) -> set[str]:
    return {
        str(c).casefold()
        for c in SKU.objects.filter(agency=agency).values_list("sku_code", flat=True)
        if c
    }


def _sku_map(agency: Agency) -> dict[str, SKU]:
    return {str(s.sku_code).casefold(): s for s in SKU.objects.filter(agency=agency) if s.sku_code}


def _existing_receiving_ids() -> list[str]:
    return list(
        OrderAuditEntry.objects.filter(order_type="receiving")
        .values_list("order_id", flat=True)
        .distinct()
    )


@transaction.atomic
def create_receiving_distribution(
    *,
    agency: Agency,
    draft: DistributionDraft,
    user=None,
    submit: bool = False,
    client_request_id: str = "",
    allow_incomplete: bool = False,
) -> CreateResult:
    """
    submit=False → черновик приёмки + план.
    submit=True → отправка менеджеру через существующий workflow + связанные OTG в draft.
    """
    request_key = str(client_request_id or "").strip()
    if request_key:
        existing = (
            ReceivingDistributionPlan.objects.select_related("agency")
            .filter(agency=agency, client_request_id=request_key)
            .first()
        )
        if existing:
            return CreateResult(
                plan=existing,
                receiving_order_id=existing.receiving_order_id,
                shipping_numbers=[
                    d.shipping_order.number
                    for d in existing.directions.select_related("shipping_order")
                    if d.shipping_order_id
                ],
                created=False,
            )

    known = _client_sku_codes(agency)
    issues = validate_distribution_draft(
        draft,
        allow_incomplete=allow_incomplete or not submit,
        known_sku_codes=known,
    )
    blocking = [i for i in issues if getattr(i, "severity", "error") != "warning"]
    if blocking:
        raise ValidationError([i.message for i in blocking])

    skus = _sku_map(agency)
    meta = dict(draft.meta or {})
    eta_at = _parse_eta(meta.get("eta_at"))
    if submit and eta_at is None:
        raise ValidationError("Укажите плановую дату и время прибытия.")
    if submit and eta_at is not None and eta_at.date() < timezone.localdate():
        raise ValidationError("Плановая дата прибытия не может быть в прошлом.")
    receiving_order_id = next_public_order_number("receiving", _existing_receiving_ids())

    items_payload = []
    expected_units = 0
    for row in draft.items:
        code = str(row.get("sku_code") or "").strip()
        sku = skus.get(code.casefold())
        qty = int(row.get("qty_total") or 0)
        expected_units += qty
        items_payload.append(
            {
                "sku_id": sku.id if sku else None,
                "sku_code": code,
                "name": str(row.get("name") or (sku.name if sku else "") or code),
                "brand": "",
                "color": "",
                "size": str(row.get("size") or ""),
                "barcode": str(row.get("barcode") or ""),
                "qty": qty,
                "comment": str(row.get("comment") or ""),
            }
        )

    submit_action = "send" if submit else "draft"
    status_value = "sent_unconfirmed" if submit else "draft"
    status_label = "Ждет подтверждения" if submit else "Черновик"
    payload = {
        "eta_at": eta_at.isoformat() if eta_at else "",
        "expected_boxes": int(meta.get("expected_boxes") or 0),
        "place_type": str(meta.get("place_type") or ""),
        "vehicle_number": str(meta.get("vehicle_number") or ""),
        "driver_phone": str(meta.get("driver_phone") or ""),
        "comment": str(meta.get("comment") or ""),
        "submit_action": submit_action,
        "status": status_value,
        "status_label": status_label,
        "items": items_payload,
        "documents": [],
        "template_files": [],
        # маркер нового сценария — складской flow не меняем
        "receiving_kind": "distribution",
        "receiving_distribution": True,
    }

    ReceivingWorkflowService.submit_receiving_order(
        order_id=receiving_order_id,
        agency=agency,
        payload=payload,
        submit_action=submit_action,
        user=user,
        submitted_at=timezone.localtime() if submit else None,
        dispatch_review=bool(submit),
    )

    try:
        plan = ReceivingDistributionPlan.objects.create(
            receiving_order_id=receiving_order_id,
            agency=agency,
            status=(
                ReceivingDistributionPlan.STATUS_SUBMITTED
                if submit
                else ReceivingDistributionPlan.STATUS_DRAFT
            ),
            client_request_id=request_key,
            comment=str(meta.get("comment") or ""),
            eta_at=eta_at,
            expected_units=expected_units,
            expected_boxes=int(meta.get("expected_boxes") or 0),
            expected_pallets=int(meta.get("expected_pallets") or 0),
            vehicle_number=str(meta.get("vehicle_number") or ""),
            driver_name=str(meta.get("driver_name") or ""),
            driver_phone=str(meta.get("driver_phone") or ""),
            created_by=user if getattr(user, "is_authenticated", False) else None,
            submitted_at=timezone.localtime() if submit else None,
        )
    except IntegrityError as exc:
        raise ValidationError("Повторная отправка: заявка с таким ключом уже создана.") from exc

    dir_by_key: dict[str, ReceivingDistributionDirection] = {}
    shipping_numbers: list[str] = []
    for idx, drow in enumerate(draft.directions):
        key = str(drow.get("key") or drow.get("title") or f"d{idx}").strip()
        kind = str(drow.get("kind") or ReceivingDistributionDirection.KIND_MARKETPLACE).strip()
        if kind == "storage" or kind == "storage_fullbox":
            kind = ReceivingDistributionDirection.KIND_STORAGE
        market_name = str(drow.get("marketplace_name") or drow.get("marketplace") or "").strip()
        market = _resolve_market(market_name)
        title = str(drow.get("title") or "").strip()
        if not title:
            if kind == ReceivingDistributionDirection.KIND_STORAGE:
                title = "Хранение FullBox"
            else:
                title = " — ".join(
                    p
                    for p in (
                        market_name,
                        str(drow.get("destination_warehouse") or "").strip(),
                    )
                    if p
                ) or key

        direction = ReceivingDistributionDirection.objects.create(
            plan=plan,
            sort_order=idx,
            kind=kind,
            title=title,
            marketplace=market,
            marketplace_name=market_name or (market.name if market else ""),
            destination_warehouse=str(drow.get("destination_warehouse") or ""),
            cluster=str(drow.get("cluster") or ""),
            supply_number=str(drow.get("supply_number") or ""),
            slot_date=parse_date(str(drow.get("slot_date") or "")) if drow.get("slot_date") else None,
            slot_time=str(drow.get("slot_time") or ""),
            delivery_type=str(drow.get("delivery_type") or ""),
            expected_boxes=int(drow.get("expected_boxes") or 0),
            expected_pallets=int(drow.get("expected_pallets") or 0),
            comment=str(drow.get("comment") or ""),
            status=ReceivingDistributionDirection.STATUS_AWAITING,
        )
        dir_by_key[key.casefold()] = direction

        if direction.needs_shipping and submit:
            so = _create_linked_shipping_draft(
                agency=agency,
                user=user,
                receiving_order_id=receiving_order_id,
                direction=direction,
            )
            direction.shipping_order = so
            direction.save(update_fields=["shipping_order", "updated_at"])
            shipping_numbers.append(so.number)

    item_by_sku: dict[str, ReceivingDistributionItem] = {}
    for row in draft.items:
        code = str(row.get("sku_code") or "").strip()
        sku = skus.get(code.casefold())
        item = ReceivingDistributionItem.objects.create(
            plan=plan,
            sku=sku,
            sku_code=code,
            barcode=str(row.get("barcode") or ""),
            name=str(row.get("name") or (sku.name if sku else "") or code),
            size=str(row.get("size") or ""),
            qty_total=int(row.get("qty_total") or 0),
            comment=str(row.get("comment") or ""),
        )
        item_by_sku[code.casefold()] = item

    dir_units: dict[int, int] = {}
    ship_lines: dict[int, dict[str, dict[str, Any]]] = {}
    for arow in draft.allocations:
        sku_key = str(arow.get("sku_code") or "").strip().casefold()
        dkey = str(arow.get("direction_key") or "").strip().casefold()
        qty = int(arow.get("qty") or 0)
        item = item_by_sku[sku_key]
        direction = dir_by_key[dkey]
        ReceivingDistributionAllocation.objects.create(
            plan=plan,
            item=item,
            direction=direction,
            qty=qty,
        )
        dir_units[direction.id] = dir_units.get(direction.id, 0) + qty
        if direction.shipping_order_id and submit:
            bucket = ship_lines.setdefault(direction.shipping_order_id, {})
            if sku_key not in bucket:
                bucket[sku_key] = {
                    "sku_code": item.sku_code,
                    "name": item.name,
                    "barcode": item.barcode,
                    "size": item.size,
                    "qty_requested": 0,
                }
            bucket[sku_key]["qty_requested"] += qty

    for direction in plan.directions.all():
        units = dir_units.get(direction.id, 0)
        if units != direction.expected_units:
            direction.expected_units = units
            direction.save(update_fields=["expected_units", "updated_at"])

    for order_id, lines in ship_lines.items():
        order = ShippingOrder.objects.get(pk=order_id)
        for line in lines.values():
            ShippingOrderItem.objects.create(order=order, **line)

    ReceivingDistributionEvent.objects.create(
        plan=plan,
        action="plan_created" if not submit else "plan_submitted",
        user=user if getattr(user, "is_authenticated", False) else None,
        role="client",
        object_type="plan",
        object_id=str(plan.id),
        new_value={
            "receiving_order_id": receiving_order_id,
            "shipping_numbers": shipping_numbers,
            "submit": submit,
        },
        source="client_form",
    )
    return CreateResult(
        plan=plan,
        receiving_order_id=receiving_order_id,
        shipping_numbers=shipping_numbers,
        created=True,
    )


def _create_linked_shipping_draft(
    *,
    agency: Agency,
    user,
    receiving_order_id: str,
    direction: ReceivingDistributionDirection,
) -> ShippingOrder:
    """Только STATUS_DRAFT — без reserve/pick/submit."""
    mp = (direction.marketplace_name or "").casefold()
    supply = str(direction.supply_number or "").strip()
    slot_time_raw = str(direction.slot_time or "").strip()
    slot_field = ShippingOrder._meta.get_field("slot_time")
    # TimeField не принимает "" — только None или HH:MM[:ss]
    if getattr(slot_field, "get_internal_type", lambda: "")() == "TimeField":
        from datetime import time as time_cls

        slot_time_value = None
        if slot_time_raw:
            parsed = parse_datetime(f"2000-01-01T{slot_time_raw}")
            if parsed:
                slot_time_value = parsed.time()
            else:
                parts = slot_time_raw.replace(".", ":").split(":")
                try:
                    slot_time_value = time_cls(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
                except (TypeError, ValueError, IndexError):
                    slot_time_value = None
    else:
        slot_time_value = slot_time_raw

    create_kwargs: dict[str, Any] = {
        "number": next_shipping_number(),
        "agency": agency,
        "created_by": user if getattr(user, "is_authenticated", False) else None,
        "status": ShippingOrder.STATUS_DRAFT,
        "delivery_type": ShippingOrder.DELIVERY_MARKETPLACE,
        "marketplace": direction.marketplace,
        "slot_date": direction.slot_date,
        "slot_time": slot_time_value,
        "destination_warehouse": direction.destination_warehouse or "",
        "wb_supply_barcode": supply if ("wb" in mp or "wildberries" in mp) else "",
        "shipping_barcode": supply,
        "supply_type": ShippingOrder.SUPPLY_BOX,
        "vehicle_type": ShippingOrder.VEHICLE_FULFILLMENT,
        "expected_boxes": int(direction.expected_boxes or 0),
        "comment": (
            f"Ожидает приемки товара по {receiving_order_id}. "
            f"Направление: {direction.title}. "
            + (f"Поставка: {supply}. " if supply else "")
            + "Автосоздание из приёмки с распределением. Резерв и отбор — только после фактической приёмки."
        ),
    }
    # На проде поле supply_number может отсутствовать (миграция shipping ещё не выкатана).
    if any(f.name == "supply_number" for f in ShippingOrder._meta.local_fields):
        create_kwargs["supply_number"] = supply if mp == "ozon" else ""
    so = ShippingOrder.objects.create(**create_kwargs)
    from audit.models import log_order_action

    log_order_action(
        action="create",
        order_id=so.number,
        order_type="shipping",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=agency,
        description=f"Отгрузка ожидает приемки ({receiving_order_id})",
        payload=order_payload(
            so,
            extra={
                "source_receiving_order_id": receiving_order_id,
                "receiving_distribution_direction_id": direction.id,
                "awaiting_receiving": True,
                "status_label_override": "Ожидает приемки",
            },
        ),
    )
    return so
