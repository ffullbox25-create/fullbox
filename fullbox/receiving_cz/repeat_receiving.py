"""Order-scoped re-receiving of shipped marks; never a global duplicate bypass."""

from collections import defaultdict

from django.db.models import Q

from audit.models import OrderAuditEntry
from fbs.models import FbsStockBalance
from marking.codes import marking_code_identity, marking_code_variants
from marking.models import MarkingCode
from orders.shipping_returns import _matching_mark_snapshot, resolve_shipping_return_order
from sklad.models import WarehouseStockSnapshot
from sku.models import Agency

from .models import ReceivingCzUnit


# Approved return intake only. All boxes in these orders are covered.
SHIPPED_MARK_INTAKES = {
    "PR-000349": "340346345482",
    "PR-000350": "341601596136",
}


def lock_shipped_mark_intake(context):
    """Serialize exception scans before checking history and adding a unit.

    Caller must run inside transaction.atomic (scan_unit does). Bind the order
    to the actual agency INN, not a client-supplied flag or the box prefix.
    """
    inn = SHIPPED_MARK_INTAKES.get(context.order_id)
    if not inn or not context.agency:
        return False
    return Agency.objects.select_for_update().filter(pk=context.agency.pk, inn=inn).first() is not None


def _used_since(code, shipped_at):
    if _matching_mark_snapshot(ReceivingCzUnit.objects.filter(accepted_at__gte=shipped_at), code):
        return True
    return MarkingCode.objects.filter(
        Q(identity_key=marking_code_identity(code)) | Q(code__in=marking_code_variants(code)),
        used_at__gte=shipped_at,
    ).exists()


def _item_key(sku_code, size):
    return str(sku_code or "").strip().casefold(), str(size or "").strip().casefold()


def _completed_bulk_receiving_evidence(context, item, code):
    """Legacy ordinary intake stored imported marks, but stock by box/SKU.

    No per-serial shipment is claimed here. Require the ENTIRE closed source
    receipt to reconcile to positive shipment events, with no remaining stock.
    This fallback is reached only after the common current-stock/duplicate
    guards and is still limited to the two explicitly approved intake orders.
    """
    mark = MarkingCode.objects.filter(
        Q(identity_key=marking_code_identity(code)) | Q(code__in=marking_code_variants(code)),
        agency=context.agency, order_type="receiving", source="import", used_at__isnull=True,
    ).exclude(order_id="").exclude(order_id=context.order_id).first()
    if mark is None or _item_key(mark.sku_code, mark.size) != _item_key(item.sku_code, item.size):
        return None
    receipt = OrderAuditEntry.objects.filter(
        agency=context.agency, order_type="receiving", order_id=mark.order_id, action="status",
    ).order_by("-id").first()
    payload = receipt.payload if receipt and isinstance(receipt.payload, dict) else {}
    if (
        payload.get("status") != "done" or payload.get("flow_closed") is not True
        or payload.get("act_state") != "closed" or payload.get("receiving_mode") != "standard"
        or mark.created_at > receipt.created_at
    ):
        return None
    expected = defaultdict(int)
    for row in payload.get("act_items") or []:
        if not isinstance(row, dict):
            return None
        try:
            value = row.get("actual_qty")
            qty = int(value)
            if qty < 0 or str(value).strip() != str(qty):
                return None
        except (TypeError, ValueError):
            return None
        if qty:
            key = _item_key(row.get("sku_code") or row.get("sku"), row.get("size"))
            if not key[0]:
                return None
            expected[key] += qty
    if not expected:
        return None
    rows = list(WarehouseStockSnapshot.objects.filter(
        agency=context.agency, source_context_type="receiving", source_context_id=mark.order_id,
    ).select_related("last_event").order_by("id"))
    if not rows:
        return None
    actual = defaultdict(int)
    events = set()
    shipments = {}
    item_rows = []
    for row in rows:
        event = row.last_event
        if (
            not row.is_archived or row.warehouse_state_code != "shipped" or row.marking_code
            or any((row.qty, row.available_qty, row.processing_reserved_qty,
                    row.shipping_reserved_qty, row.other_reserved_qty))
            or event is None or event.pk in events or event.agency_id != context.agency.pk
            or event.event_type != "shipped" or event.stock_context_type != "shipping"
            or event.qty <= 0 or event.occurred_at <= receipt.created_at
        ):
            return None
        number = event.stock_context_id
        if number not in shipments:
            shipments[number] = resolve_shipping_return_order(agency=context.agency, number=number)
        if shipments[number] is None:
            return None
        events.add(event.pk)
        key = _item_key(row.sku_code, row.size)
        actual[key] += event.qty
        # Imported size "25" may be the same SKU/barcode as warehouse size
        # "25(RUS 40-42)". An exact barcode is required for that legacy alias.
        if key[0] == _item_key(item.sku_code, item.size)[0] and (
            key[1] == _item_key(item.sku_code, item.size)[1]
            or (mark.barcode and row.barcode == mark.barcode)
        ):
            item_rows.append(row)
    if actual != expected or not item_rows:
        return None
    earliest_shipment = min(row.last_event.occurred_at for row in rows)
    if _used_since(code, earliest_shipment):
        return None
    return {
        "evidence_type": "completed_receiving_bulk_shipment",
        "shipping_order_number": ", ".join(sorted(shipments)),
        "previous_receiving_order_id": mark.order_id,
        "previous_mark_registry_id": mark.pk,
        "source_receipt_qty": sum(expected.values()),
        "source_shipped_qty": sum(actual.values()),
        "source_receipt_status_entry_id": receipt.pk,
        "matching_stock_snapshot_ids": [row.pk for row in item_rows],
        "matching_shipping_event_ids": [row.last_event_id for row in item_rows],
    }


def shipped_mark_evidence(context, item, code):
    """Return shipment proof only when the serialized unit can safely re-enter.

    Shipped snapshots may have qty=0. Proof is their positive shipment event
    plus a shipped/partially shipped document, not an old receiving record.
    This function is read-only; lock_shipped_mark_intake must precede writes.
    """
    if SHIPPED_MARK_INTAKES.get(context.order_id) != str(context.agency.inn or "").strip():
        return None
    units = ReceivingCzUnit.objects.all()
    if _matching_mark_snapshot(units.filter(order_id=context.order_id), code):
        return None
    if _matching_mark_snapshot(units.exclude(agency_id=context.agency.pk), code):
        return None
    live = WarehouseStockSnapshot.objects.filter(is_archived=False).filter(
        Q(qty__gt=0) | Q(available_qty__gt=0) | Q(processing_reserved_qty__gt=0)
        | Q(shipping_reserved_qty__gt=0) | Q(other_reserved_qty__gt=0)
    )
    if _matching_mark_snapshot(live, code):
        return None
    if _matching_mark_snapshot(FbsStockBalance.objects.filter(qty__gt=0), code):
        return None
    source = _matching_mark_snapshot(
        WarehouseStockSnapshot.objects.filter(
            agency=context.agency, is_archived=True, warehouse_state_code="shipped",
            last_event__agency=context.agency, last_event__event_type="shipped",
            last_event__qty__gt=0, last_event__stock_context_type="shipping",
        ).select_related("last_event").order_by("-last_event__occurred_at", "-id"), code,
    )
    if source is None:
        return _completed_bulk_receiving_evidence(context, item, code)
    if str(source.sku_code).strip().casefold() != item.sku_code.strip().casefold():
        return None
    if str(source.size or "").strip().casefold() != item.size.strip().casefold():
        return None
    shipment = resolve_shipping_return_order(
        agency=context.agency, number=source.last_event.stock_context_id,
    )
    if shipment is None:
        return None
    # Includes unfinished intake: a newly scanned unit may not have a stock
    # snapshot yet. Reusing an older shipment must not allow a third intake.
    if _used_since(code, source.last_event.occurred_at):
        return None
    return {
        "shipping_order_number": shipment.number,
        "shipping_event_id": source.last_event_id,
        "stock_snapshot_id": source.pk,
        "previous_receiving_order_id": source.source_context_id,
        "previous_box_code": source.container_code,
    }
