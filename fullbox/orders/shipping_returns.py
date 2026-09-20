from __future__ import annotations

import re
from collections import defaultdict

from django.db.models import Q

from audit.models import OrderAuditEntry
from marking.codes import (
    marking_code_identity,
    marking_code_variants as canonical_marking_code_variants,
)
from shipping.models import ShippingOrder, ShippingOrderItem
from sklad.models import WarehouseStockSnapshot


SHIPPING_RETURN_GOODS_TYPE = "votg"
SHIPPING_RETURN_GOODS_TYPE_LABEL = "Возврат с отгрузки"
SHIPPING_RETURN_SOURCE_STATUSES = {
    ShippingOrder.STATUS_SHIPPED,
    ShippingOrder.STATUS_PARTIAL,
}


def _qty(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def shipping_return_item_key(sku_code, size="") -> tuple[str, str]:
    return (
        str(sku_code or "").strip().casefold(),
        str(size or "").strip().casefold(),
    )


def normalize_shipping_order_number(value) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    legacy = re.fullmatch(r"0*(\d+)_OTG", raw)
    if legacy:
        return f"OTG-{int(legacy.group(1)):06d}"
    prefixed = re.fullmatch(r"(?:OTG|SO)[-_]?0*(\d+)", raw)
    if prefixed:
        return f"OTG-{int(prefixed.group(1)):06d}"
    if raw.isdigit():
        return f"OTG-{int(raw):06d}"
    return raw


def resolve_shipping_return_order(*, agency, number, for_update: bool = False):
    normalized = normalize_shipping_order_number(number)
    if not normalized or agency is None:
        return None
    queryset = ShippingOrder.objects.filter(
        agency=agency,
        status__in=SHIPPING_RETURN_SOURCE_STATUSES,
    )
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.filter(number__iexact=normalized).first()


def completed_shipping_return_quantities(
    source_order_number: str,
    *,
    exclude_receiving_order_id: str = "",
) -> dict[tuple[str, str], int]:
    source_number = normalize_shipping_order_number(source_order_number)
    if not source_number:
        return {}
    entries = list(
        OrderAuditEntry.objects.filter(
            order_type="receiving",
            payload__act="receiving",
            payload__shipping_return_order_number=source_number,
        ).order_by("created_at", "id")
    )
    latest_by_order: dict[str, OrderAuditEntry] = {}
    excluded = str(exclude_receiving_order_id or "").strip()
    for entry in entries:
        if excluded and str(entry.order_id or "").strip() == excluded:
            continue
        latest_by_order[str(entry.order_id or "").strip()] = entry

    totals: dict[tuple[str, str], int] = defaultdict(int)
    for entry in latest_by_order.values():
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        if payload.get("act_state") not in {"", None, "closed"} and not payload.get("flow_closed"):
            continue
        for item in payload.get("act_items") or []:
            if not isinstance(item, dict):
                continue
            key = shipping_return_item_key(
                item.get("sku_code") or item.get("sku"),
                item.get("size"),
            )
            if not key[0]:
                continue
            totals[key] += _qty(
                item.get("actual_qty") if item.get("actual_qty") is not None else item.get("qty")
            )
    return dict(totals)


def shipping_return_source_items(
    source_order: ShippingOrder,
    *,
    exclude_receiving_order_id: str = "",
) -> list[dict]:
    returned = completed_shipping_return_quantities(
        source_order.number,
        exclude_receiving_order_id=exclude_receiving_order_id,
    )
    rows: dict[tuple[str, str], dict] = {}
    source_rows = (
        ShippingOrderItem.objects.filter(order=source_order, qty_shipped__gt=0)
        .select_related("sku")
        .order_by("id")
    )
    for source in source_rows:
        key = shipping_return_item_key(source.sku_code, source.size)
        if not key[0]:
            continue
        sku = source.sku
        row = rows.setdefault(
            key,
            {
                "sku_id": int(sku.id) if sku else None,
                "sku_code": str(source.sku_code or "").strip(),
                "sku": str(source.sku_code or "").strip(),
                "name": str(source.name or "").strip(),
                "brand": str(getattr(sku, "brand", "") or "").strip(),
                "color": str(getattr(sku, "color", "") or "").strip(),
                "size": str(source.size or "").strip(),
                "barcode": str(source.barcode or "").strip(),
                "qty": 0,
                "source_shipped_qty": 0,
                "source_returned_qty": 0,
                "source_goods_type": str(source.goods_type or "").strip(),
                "goods_type": SHIPPING_RETURN_GOODS_TYPE,
                "shipping_return_source": True,
                "comment": "",
            },
        )
        row["source_shipped_qty"] += _qty(source.qty_shipped)
        if not row["barcode"]:
            row["barcode"] = str(source.barcode or "").strip()
        if not row["source_goods_type"]:
            row["source_goods_type"] = str(source.goods_type or "").strip()

    result = []
    for key, row in rows.items():
        returned_qty = _qty(returned.get(key))
        available_qty = max(_qty(row["source_shipped_qty"]) - returned_qty, 0)
        row["source_returned_qty"] = returned_qty
        row["qty"] = available_qty
        if available_qty > 0:
            result.append(row)
    return result


def validate_shipping_return_items(
    source_order: ShippingOrder,
    actual_items: list[dict],
    *,
    exclude_receiving_order_id: str = "",
    allowed_overage_by_item: dict[tuple[str, str], int] | None = None,
) -> tuple[bool, str, dict]:
    allowed_rows = shipping_return_source_items(
        source_order,
        exclude_receiving_order_id=exclude_receiving_order_id,
    )
    allowed = {
        shipping_return_item_key(row.get("sku_code"), row.get("size")): _qty(row.get("qty"))
        for row in allowed_rows
    }
    actual: dict[tuple[str, str], int] = defaultdict(int)
    for item in actual_items or []:
        if not isinstance(item, dict):
            continue
        key = shipping_return_item_key(
            item.get("sku_code") or item.get("sku"),
            item.get("size"),
        )
        qty = _qty(item.get("actual_qty") if item.get("actual_qty") is not None else item.get("qty"))
        if key[0] and qty > 0:
            actual[key] += qty

    for key, actual_qty in actual.items():
        if key not in allowed:
            return False, "shipping_return_item_not_in_source", {
                "sku_code": key[0],
                "size": key[1],
                "actual_qty": actual_qty,
                "allowed_qty": 0,
            }
        allowed_overage = _qty((allowed_overage_by_item or {}).get(key))
        allowed_qty = allowed[key] + allowed_overage
        if actual_qty > allowed_qty:
            return False, "shipping_return_qty_exceeded", {
                "sku_code": key[0],
                "size": key[1],
                "actual_qty": actual_qty,
                "allowed_qty": allowed_qty,
                "source_allowed_qty": allowed[key],
                "new_mark_overage_qty": allowed_overage,
            }
    return True, "", {}


def marking_code_variants(code: str) -> set[str]:
    return canonical_marking_code_variants(code)


def _matching_mark_snapshot(queryset, marking_code: str):
    """Match a stored ЧЗ by serialized-item identity, not by scan representation.

    A warehouse scanner may return only ``01 + GTIN + 21 + serial`` while an
    earlier receiving stored the same Data Matrix together with its 91/92
    crypto tail.  Exact string matching treats those values as different even
    though they identify the same physical unit.
    """

    identity = marking_code_identity(marking_code)
    variants = marking_code_variants(marking_code)
    if not identity or not variants:
        return None

    lookup = Q(marking_code__in=variants)
    if identity.startswith("01") and len(identity) >= 18 and identity[16:18] == "21":
        lookup |= Q(marking_code__startswith=identity)
        lookup |= Q(marking_code__startswith=f"]d2{identity}")
        lookup |= Q(marking_code__startswith=f"]D2{identity}")

    for snapshot in queryset.filter(lookup).iterator(chunk_size=200):
        if marking_code_identity(snapshot.marking_code) == identity:
            return snapshot
    return None


def marking_code_gtin(code: str) -> str:
    text = str(code or "").strip()
    text = re.sub(r"_x001d_", "\x1d", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*gs\s*>", "\x1d", text, flags=re.IGNORECASE)
    text = text.replace("\\u001d", "\x1d").replace("\\x1d", "\x1d").replace("\\x001d", "\x1d")
    if text[:3].casefold() == "]d2":
        text = text[3:]
    if len(text) >= 16 and text.startswith("01") and text[2:16].isdigit():
        return text[2:16]
    return ""


def _product_barcode_gtin(barcode: str) -> str:
    value = str(barcode or "").strip()
    if value.isdigit() and len(value) in {8, 12, 13, 14}:
        return value.zfill(14)
    return ""


def shipping_return_source_item_gtins(
    source_order: ShippingOrder,
    *,
    sku_code: str,
    size: str = "",
) -> set[str]:
    snapshots = WarehouseStockSnapshot.objects.filter(
        agency=source_order.agency,
        is_archived=True,
        qty__gt=0,
        warehouse_state_code="shipped",
        last_event__stock_context_type="shipping",
        last_event__stock_context_id=source_order.number,
        sku_code__iexact=str(sku_code or "").strip(),
    )
    normalized_size = str(size or "").strip()
    if normalized_size:
        snapshots = snapshots.filter(size__iexact=normalized_size)
    gtins = set()
    for marking_code, barcode in snapshots.values_list("marking_code", "barcode"):
        gtin = marking_code_gtin(marking_code) or _product_barcode_gtin(barcode)
        if gtin:
            gtins.add(gtin)

    source_items = ShippingOrderItem.objects.filter(
        order=source_order,
        qty_shipped__gt=0,
        sku_code__iexact=str(sku_code or "").strip(),
    )
    if normalized_size:
        source_items = source_items.filter(size__iexact=normalized_size)
    for barcode in source_items.values_list("barcode", flat=True):
        gtin = _product_barcode_gtin(barcode)
        if gtin:
            gtins.add(gtin)
    return gtins


def shipping_return_mark_has_stock_history(source_order: ShippingOrder, marking_code: str) -> bool:
    return _matching_mark_snapshot(
        WarehouseStockSnapshot.objects.filter(agency=source_order.agency),
        marking_code,
    ) is not None


def shipping_return_source_mark_snapshot(source_order: ShippingOrder, marking_code: str):
    return _matching_mark_snapshot(
        WarehouseStockSnapshot.objects.select_related("sku_ref", "last_event")
        .filter(
            agency=source_order.agency,
            is_archived=True,
            qty__gt=0,
            warehouse_state_code="shipped",
            last_event__stock_context_type="shipping",
            last_event__stock_context_id=source_order.number,
        )
        .order_by("-updated_at", "-id"),
        marking_code,
    )


def shipping_return_mark_is_live(source_order: ShippingOrder, marking_code: str) -> bool:
    return _matching_mark_snapshot(
        WarehouseStockSnapshot.objects.filter(
            agency=source_order.agency,
            is_archived=False,
            qty__gt=0,
        ),
        marking_code,
    ) is not None


def search_shipping_return_orders(*, agency, query: str, limit: int = 20):
    if agency is None:
        return []
    text = str(query or "").strip()
    normalized = normalize_shipping_order_number(text)
    digits = "".join(ch for ch in text if ch.isdigit())
    number_filter = Q()
    if normalized:
        number_filter |= Q(number__icontains=normalized)
    if digits:
        number_filter |= Q(number__icontains=digits.lstrip("0") or "0")
    queryset = ShippingOrder.objects.filter(
        agency=agency,
        status__in=SHIPPING_RETURN_SOURCE_STATUSES,
    )
    if text:
        queryset = queryset.filter(number_filter)
    return list(queryset.prefetch_related("items").order_by("-shipped_at", "-created_at")[:limit])
