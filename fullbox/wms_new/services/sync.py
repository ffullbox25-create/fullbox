from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
import re

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from audit.models import OrderAuditEntry
from fbs.models import (
    FbsInventoryLine,
    FbsInventoryScan,
    FbsInventorySession,
    FbsHandoverBatch,
    FbsHandoverOrder,
    FbsOrder,
    FbsPickBatch,
    FbsPickRestockRequest,
    FbsStockBalance,
)
from marking.models import MarkingCode
from logistics.models import LogisticsTrip
from shipping.models import ShippingOrder
from sklad.models import WarehouseContainer, WarehouseEvent, WarehouseStockSnapshot
from sku.models import SKU
from todo.models import Task
from warehouse_goods.models import GoodsExtraFieldDefinition, GoodsExtraFieldValue

from ..models import (
    WmsNewOrder,
    WmsNewOrderItem,
    WmsNewAcceptance,
    WmsNewBox,
    WmsNewBoxItem,
    WmsNewDocument,
    WmsNewDocumentItem,
    WmsNewExtraFieldDefinition,
    WmsNewExtraFieldValue,
    WmsNewInventoryLine,
    WmsNewInventoryScan,
    WmsNewInventorySession,
    WmsNewMarkingCode,
    WmsNewLogisticsManifest,
    WmsNewLogisticsOrder,
    WmsNewLogisticsPackage,
    WmsNewLogisticsRouteRule,
    WmsNewMovement,
    WmsNewProduct,
    WmsNewReturn,
    WmsNewShipment,
    WmsNewShipmentBox,
    WmsNewShipmentOrder,
    WmsNewSyncRun,
    WmsNewTask,
    WmsNewWave,
    WmsNewWaveOrder,
)


ORDER_STATUS_MAP = {
    FbsOrder.STATUS_RECEIVED: WmsNewOrder.STATUS_NEW,
    FbsOrder.STATUS_VALIDATION_FAILED: WmsNewOrder.STATUS_EXCEPTION,
    FbsOrder.STATUS_AWAITING_STOCK: WmsNewOrder.STATUS_AWAITING_STOCK,
    FbsOrder.STATUS_RESERVED: WmsNewOrder.STATUS_RESERVED,
    FbsOrder.STATUS_QUEUED_FOR_PICK: WmsNewOrder.STATUS_QUEUED,
    FbsOrder.STATUS_PICKING: WmsNewOrder.STATUS_PICKING,
    FbsOrder.STATUS_PICKED: WmsNewOrder.STATUS_PICKED,
    FbsOrder.STATUS_READY_FOR_HANDOVER: WmsNewOrder.STATUS_READY,
    FbsOrder.STATUS_HANDED_OVER: WmsNewOrder.STATUS_HANDED_OVER,
    FbsOrder.STATUS_DELIVERED: WmsNewOrder.STATUS_DONE,
    FbsOrder.STATUS_CANCELLED: WmsNewOrder.STATUS_CANCELLED,
    FbsOrder.STATUS_RETURN_PENDING: WmsNewOrder.STATUS_RETURN_PENDING,
    FbsOrder.STATUS_RETURNED: WmsNewOrder.STATUS_RETURNED,
    FbsOrder.STATUS_EXCEPTION: WmsNewOrder.STATUS_EXCEPTION,
}

TASK_STATUS_MAP = {
    "backlog": WmsNewTask.STATUS_NEW,
    "in_progress": WmsNewTask.STATUS_IN_PROGRESS,
    "blocked": WmsNewTask.STATUS_BLOCKED,
    "done": WmsNewTask.STATUS_DONE,
}

WAVE_STATUS_MAP = {
    FbsPickBatch.STATUS_QUEUED: WmsNewWave.STATUS_QUEUED,
    FbsPickBatch.STATUS_IN_PROGRESS: WmsNewWave.STATUS_IN_PROGRESS,
    FbsPickBatch.STATUS_VERIFICATION: WmsNewWave.STATUS_VERIFICATION,
    FbsPickBatch.STATUS_DONE: WmsNewWave.STATUS_DONE,
    FbsPickBatch.STATUS_CANCELED: WmsNewWave.STATUS_CANCELLED,
}

SHIPMENT_STATUS_MAP = {
    FbsHandoverBatch.STATUS_OPEN: WmsNewShipment.STATUS_NEW,
    FbsHandoverBatch.STATUS_READY: WmsNewShipment.STATUS_CHECKED,
    FbsHandoverBatch.STATUS_DISPATCHED: WmsNewShipment.STATUS_IN_TRANSIT,
    FbsHandoverBatch.STATUS_ACCEPTED: WmsNewShipment.STATUS_ACCEPTED,
    FbsHandoverBatch.STATUS_PROBLEM: WmsNewShipment.STATUS_REJECTED,
}

RETURN_STATUS_MAP = {
    FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE: WmsNewReturn.STATUS_WAITING,
    FbsPickRestockRequest.STATUS_QUEUED: WmsNewReturn.STATUS_QUEUED,
    FbsPickRestockRequest.STATUS_IN_PROGRESS: WmsNewReturn.STATUS_IN_PROGRESS,
    FbsPickRestockRequest.STATUS_COMPLETED: WmsNewReturn.STATUS_COMPLETED,
    FbsPickRestockRequest.STATUS_FAILED: WmsNewReturn.STATUS_FAILED,
    FbsPickRestockRequest.STATUS_CANCELED: WmsNewReturn.STATUS_CANCELLED,
}


def _payload_value(payload, keys: tuple[str, ...]) -> str:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
        for value in payload.values():
            nested = _payload_value(value, keys)
            if nested:
                return nested
    elif isinstance(payload, list):
        for value in payload[:20]:
            nested = _payload_value(value, keys)
            if nested:
                return nested
    return ""


def _availability_maps(source_orders) -> tuple[dict, dict]:
    pairs = {
        (order.profile.agency_id, str(item.barcode or "").strip())
        for order in source_orders
        for item in order.items.all()
        if item.barcode
    }
    agency_ids = {agency_id for agency_id, _barcode in pairs}
    barcodes = {barcode for _agency_id, barcode in pairs}
    fbs_map = defaultdict(lambda: {"qty": 0, "available": 0, "reserved": 0})
    general_map = defaultdict(lambda: {"qty": 0, "available": 0, "reserved": 0})
    if not pairs:
        return fbs_map, general_map

    for row in (
        FbsStockBalance.objects.filter(agency_id__in=agency_ids, barcode__in=barcodes)
        .values("agency_id", "barcode")
        .annotate(qty=Sum("qty"), available=Sum("available_qty"), reserved=Sum("reserved_qty"))
    ):
        key = (int(row["agency_id"]), str(row["barcode"] or "").strip())
        fbs_map[key] = {
            "qty": int(row["qty"] or 0),
            "available": int(row["available"] or 0),
            "reserved": int(row["reserved"] or 0),
        }

    for row in (
        WarehouseStockSnapshot.objects.filter(
            agency_id__in=agency_ids,
            barcode__in=barcodes,
            is_archived=False,
            is_in_vehicle=False,
            qty__gt=0,
        )
        .values("agency_id", "barcode")
        .annotate(
            qty=Sum("qty"),
            available=Sum("available_qty"),
            processing=Sum("processing_reserved_qty"),
            shipping=Sum("shipping_reserved_qty"),
            other=Sum("other_reserved_qty"),
        )
    ):
        key = (int(row["agency_id"]), str(row["barcode"] or "").strip())
        general_map[key] = {
            "qty": int(row["qty"] or 0),
            "available": int(row["available"] or 0),
            "reserved": int(row["processing"] or 0)
            + int(row["shipping"] or 0)
            + int(row["other"] or 0),
        }
    return fbs_map, general_map


def _availability_snapshot(source_order, fbs_map, general_map) -> tuple[str, list[dict]]:
    groups = {}
    for item in source_order.items.all():
        barcode = str(item.barcode or "").strip()
        group = groups.setdefault(
            barcode or f"item:{item.id}",
            {
                "name": item.product_name or item.external_sku or "Товар",
                "barcode": barcode,
                "required": 0,
            },
        )
        group["required"] += int(item.quantity or 0)

    rows = []
    order_state = "ready"
    for group in groups.values():
        key = (source_order.profile.agency_id, group["barcode"])
        fbs = fbs_map[key]
        general = general_map[key]
        required = int(group["required"] or 0)
        available = int(fbs["available"] or 0)
        general_available = int(general["available"] or 0)
        if group["barcode"] and available >= required:
            state = "ready"
        elif group["barcode"] and available + general_available >= required:
            state = "risk"
        else:
            state = "unavailable"
        if state == "unavailable":
            order_state = "unavailable"
        elif state == "risk" and order_state != "unavailable":
            order_state = "risk"
        rows.append(
            {
                "name": group["name"],
                "barcode": group["barcode"],
                "required": required,
                "storage": int(fbs["qty"] or 0) + int(general["qty"] or 0),
                "total": int(fbs["qty"] or 0) + int(general["qty"] or 0),
                "transit": 0,
                "reserve": int(fbs["reserved"] or 0) + int(general["reserved"] or 0),
                "state": state,
            }
        )
    if not rows:
        return "unavailable", []
    return order_state, rows


def sync_orders(*, limit: int | None = None) -> dict[str, int]:
    queryset = (
        FbsOrder.objects.select_related("profile__agency")
        .prefetch_related("items")
        .order_by("id")
    )
    if limit:
        queryset = queryset[:limit]
    source_orders = list(queryset)
    fbs_map, general_map = _availability_maps(source_orders)
    imported = 0
    updated = 0
    now = timezone.now()
    with transaction.atomic():
        for source in source_orders:
            payload = source.raw_payload if isinstance(source.raw_payload, dict) else {}
            availability_state, availability_snapshot = _availability_snapshot(
                source, fbs_map, general_map
            )
            defaults = {
                "agency_id": source.profile.agency_id,
                "source_profile_id": source.profile_id,
                "marketplace": source.profile.marketplace,
                "integration_name": source.profile.name or source.profile.get_marketplace_display(),
                "external_order_id": source.external_order_id,
                "delivery_type": _payload_value(
                    payload,
                    ("delivery_type_name", "delivery_method_name", "deliveryType", "delivery_method"),
                )
                or f"{source.profile.get_marketplace_display()} FBS",
                "tracking_number": _payload_value(
                    payload,
                    ("tracking_number", "track_number", "trackingNumber", "tracking_number_id"),
                ),
                "source_status": source.internal_status,
                "ordered_at": source.ordered_at,
                "cutoff_at": source.cutoff_at,
                "source_created_at": source.ordered_at or source.imported_at,
                "source_updated_at": source.updated_at,
                "last_synced_at": now,
                "availability_state": availability_state,
                "availability_snapshot": availability_snapshot,
                "source_snapshot": {
                    "profile_id": source.profile_id,
                    "marketplace_status": source.marketplace_status,
                    "marketplace_substatus": source.marketplace_substatus,
                    "hold_reason": source.hold_reason,
                    "problem_reason": source.problem_reason,
                    "raw_payload": payload,
                },
            }
            if str(defaults["delivery_type"]).strip().lower() in {"fbs", "seller"}:
                defaults["delivery_type"] = f"{source.profile.get_marketplace_display()} FBS"
            target = WmsNewOrder.objects.filter(source_order_id=source.id).first()
            if target is None:
                target = WmsNewOrder.objects.create(
                    source_order_id=source.id,
                    status=ORDER_STATUS_MAP.get(source.internal_status, WmsNewOrder.STATUS_EXCEPTION),
                    **defaults,
                )
                imported += 1
            else:
                for field, value in defaults.items():
                    setattr(target, field, value)
                if target.pilot_revision == 0:
                    target.status = ORDER_STATUS_MAP.get(
                        source.internal_status, WmsNewOrder.STATUS_EXCEPTION
                    )
                target.save()
                updated += 1

            seen_lines = []
            for source_item in source.items.all():
                line_id = source_item.external_line_id or str(source_item.id)
                seen_lines.append(line_id)
                WmsNewOrderItem.objects.update_or_create(
                    order=target,
                    external_line_id=line_id,
                    defaults={
                        "source_item_id": source_item.id,
                        "sku_id": source_item.sku_id,
                        "external_sku": source_item.external_sku,
                        "barcode": source_item.barcode,
                        "product_name": source_item.product_name,
                        "quantity": source_item.quantity,
                        "requirements": source_item.requirements,
                        "source_snapshot": source_item.raw_payload,
                    },
                )
            target.items.exclude(external_line_id__in=seen_lines).delete()
    return {"imported": imported, "updated": updated}


def sync_waves(*, limit: int | None = None) -> dict[str, int]:
    queryset = (
        FbsPickBatch.objects.select_related(
            "agency", "assigned_to", "created_by", "workstation", "cart"
        )
        .prefetch_related("tasks__exceptions")
        .order_by("id")
    )
    if limit:
        queryset = queryset[:limit]
    source_waves = list(queryset)
    source_order_ids = {
        task.order_id for source in source_waves for task in source.tasks.all()
    }
    target_order_ids = dict(
        WmsNewOrder.objects.filter(source_order_id__in=source_order_ids).values_list(
            "source_order_id", "id"
        )
    )
    imported = 0
    updated = 0
    linked = 0
    now = timezone.now()
    with transaction.atomic():
        for source in source_waves:
            tasks = list(source.tasks.all())
            has_problems = any(
                exception.status == "open"
                for task in tasks
                for exception in task.exceptions.all()
            )
            defaults = {
                "agency_id": source.agency_id,
                "planned_orders": len(tasks),
                "planned_units": source.planned_qty,
                "picked_units": source.picked_qty,
                "created_by_id": source.created_by_id,
                "assigned_to_id": source.assigned_to_id,
                "workstation_name": source.workstation.name if source.workstation_id else "",
                "cart_name": source.cart.name if source.cart_id else "",
                "source_status": source.status,
                "source_created_at": source.created_at,
                "source_updated_at": source.updated_at,
                "last_synced_at": now,
                "started_at": source.started_at,
                "completed_at": source.completed_at or source.canceled_at,
                "source_snapshot": {
                    "source_batch_id": source.id,
                    "workstation_id": source.workstation_id,
                    "cart_id": source.cart_id,
                    "verification_assigned_to_id": source.verification_assigned_to_id,
                    "claimed_at": source.claimed_at.isoformat() if source.claimed_at else "",
                    "picking_completed_at": (
                        source.picking_completed_at.isoformat()
                        if source.picking_completed_at
                        else ""
                    ),
                    "has_problems": has_problems,
                    "tasks": [
                        {
                            "source_task_id": task.id,
                            "source_order_id": task.order_id,
                            "status": task.status,
                            "planned_quantity": task.planned_qty,
                            "picked_quantity": task.picked_qty,
                        }
                        for task in tasks
                    ],
                },
            }
            target = WmsNewWave.objects.filter(source_batch_id=source.id).first()
            if target is None:
                target = WmsNewWave.objects.create(
                    source_batch_id=source.id,
                    number=f"FB-{source.id}",
                    status=WAVE_STATUS_MAP.get(source.status, WmsNewWave.STATUS_CANCELLED),
                    **defaults,
                )
                imported += 1
            else:
                for field, value in defaults.items():
                    setattr(target, field, value)
                if target.pilot_revision == 0:
                    target.status = WAVE_STATUS_MAP.get(
                        source.status, WmsNewWave.STATUS_CANCELLED
                    )
                target.save()
                updated += 1

            if target.pilot_revision == 0:
                seen_order_ids = []
                for sequence, source_task in enumerate(tasks, start=1):
                    target_order_id = target_order_ids.get(source_task.order_id)
                    if not target_order_id:
                        continue
                    seen_order_ids.append(target_order_id)
                    WmsNewWaveOrder.objects.update_or_create(
                        wave=target,
                        order_id=target_order_id,
                        defaults={"sequence": sequence},
                    )
                    linked += 1
                target.wave_orders.exclude(order_id__in=seen_order_ids).delete()
    return {"imported": imported, "updated": updated, "linked": linked}


def sync_shipments(*, limit: int | None = None) -> dict[str, int]:
    queryset = (
        FbsHandoverBatch.objects.select_related(
            "profile__agency", "created_by", "dispatched_by"
        )
        .prefetch_related(
            "order_assignments__order",
            "boxes__orders__order",
            "boxes__orders__verified_label",
        )
        .order_by("id")
    )
    if limit:
        queryset = queryset[:limit]
    source_shipments = list(queryset)
    source_order_ids = {
        assignment.order_id
        for source in source_shipments
        for assignment in source.order_assignments.all()
    }
    target_orders = {
        item.source_order_id: item
        for item in WmsNewOrder.objects.filter(source_order_id__in=source_order_ids)
        .prefetch_related("items__sku")
    }
    imported = 0
    updated = 0
    linked = 0
    now = timezone.now()
    with transaction.atomic():
        for source in source_shipments:
            assignments = list(source.order_assignments.all())
            handover_orders = {
                handover.order_id: handover
                for box in source.boxes.all()
                for handover in box.orders.all()
            }
            marketplace_label = source.profile.get_marketplace_display()
            defaults = {
                "agency_id": source.profile.agency_id,
                "source_profile_id": source.profile_id,
                "delivery_type": f"{marketplace_label} FBS",
                "integration_name": source.profile.name,
                "external_supply_id": source.external_supply_id,
                "external_name": source.external_name,
                "marketplace_state": source.marketplace_state,
                "created_by_id": source.created_by_id,
                "dispatched_by_id": source.dispatched_by_id,
                "dispatched_at": source.dispatched_at,
                "accepted_at": source.accepted_at,
                "source_created_at": source.created_at,
                "source_updated_at": source.updated_at,
                "last_synced_at": now,
                "source_snapshot": {
                    "compatibility_key": source.compatibility_key,
                    "supply_qr_code": source.supply_qr_code,
                    "supply_label_format": source.supply_label_format,
                    "marketplace_payload": source.marketplace_payload,
                },
            }
            target = WmsNewShipment.objects.filter(source_batch_id=source.id).first()
            if target is None:
                target = WmsNewShipment.objects.create(
                    source_batch_id=source.id,
                    status=SHIPMENT_STATUS_MAP.get(source.status, WmsNewShipment.STATUS_REJECTED),
                    **defaults,
                )
                imported += 1
            else:
                for field, value in defaults.items():
                    setattr(target, field, value)
                if target.pilot_revision == 0:
                    target.status = SHIPMENT_STATUS_MAP.get(
                        source.status, WmsNewShipment.STATUS_REJECTED
                    )
                target.save()
                updated += 1

            if target.pilot_revision != 0:
                continue
            box_targets = {}
            seen_box_ids = []
            for source_box in source.boxes.all():
                seen_box_ids.append(source_box.id)
                target_box, _ = WmsNewShipmentBox.objects.update_or_create(
                    source_box_id=source_box.id,
                    defaults={
                        "shipment": target,
                        "qr_code": source_box.qr_code,
                        "external_box_id": source_box.external_box_id,
                        "status": source_box.status,
                        "problem_reason": source_box.problem_reason,
                        "source_snapshot": {
                            "label_format": source_box.label_format,
                            "label_hash": source_box.label_hash,
                            "label_ready_at": (
                                source_box.label_ready_at.isoformat()
                                if source_box.label_ready_at
                                else ""
                            ),
                        },
                    },
                )
                box_targets[source_box.id] = target_box
            target.boxes.filter(is_manual=False).exclude(source_box_id__in=seen_box_ids).delete()

            seen_order_ids = []
            total_items = 0
            total_weight = Decimal("0")
            for assignment in assignments:
                order = target_orders.get(assignment.order_id)
                if order is None:
                    continue
                handover = handover_orders.get(assignment.order_id)
                quantity = 0
                order_weight = Decimal("0")
                for item in order.items.all():
                    item_quantity = max(1, int(item.quantity or 1))
                    quantity += item_quantity
                    sku = item.sku
                    unit_weight = Decimal("0")
                    if sku is not None:
                        unit_weight = Decimal(
                            str(
                                sku.weight_gross_kg
                                or sku.weight_net_kg
                                or sku.weight_kg
                                or 0
                            )
                        )
                    order_weight += unit_weight * item_quantity
                verification_status = WmsNewShipmentOrder.VERIFY_NOT_CHECKED
                if handover and handover.verified_at:
                    verification_status = WmsNewShipmentOrder.VERIFY_CHECKED
                link, _ = WmsNewShipmentOrder.objects.update_or_create(
                    shipment=target,
                    order=order,
                    defaults={
                        "box": box_targets.get(handover.box_id) if handover else None,
                        "source_assignment_id": assignment.id,
                        "source_handover_order_id": handover.id if handover else None,
                        "assignment_status": assignment.status,
                        "verification_status": verification_status,
                        "sticker_number": order.tracking_number,
                        "weight_kg": order_weight,
                        "source_snapshot": {
                            "assignment_error": assignment.error,
                            "handover_status": handover.status if handover else "",
                            "verified_label_id": handover.verified_label_id if handover else None,
                        },
                        "added_by_id": assignment.assigned_by_id,
                        "verified_by_id": handover.verified_by_id if handover else None,
                        "verified_at": handover.verified_at if handover else None,
                    },
                )
                seen_order_ids.append(order.id)
                total_items += quantity
                total_weight += order_weight
                linked += 1
            target.shipment_orders.exclude(order_id__in=seen_order_ids).delete()
            target.order_count = len(seen_order_ids)
            target.item_count = total_items
            target.total_weight_kg = total_weight
            target.save(
                update_fields=("order_count", "item_count", "total_weight_kg", "updated_at")
            )
    return {"imported": imported, "updated": updated, "linked": linked}


def sync_returns(*, limit: int | None = None) -> dict[str, int]:
    queryset = (
        FbsPickRestockRequest.objects.filter(order__isnull=False)
        .select_related(
            "order__profile__agency",
            "order__handover_assignment__batch",
            "created_by",
            "assigned_to",
        )
        .order_by("id")
    )
    if limit:
        queryset = queryset[:limit]
    source_returns = list(queryset)
    target_orders = {
        order.source_order_id: order
        for order in WmsNewOrder.objects.filter(
            source_order_id__in=[item.order_id for item in source_returns]
        )
    }
    imported = 0
    updated = 0
    linked = 0
    now = timezone.now()
    with transaction.atomic():
        for source in source_returns:
            target_order = target_orders.get(source.order_id)
            if target_order is None:
                continue
            try:
                shipped_at = source.order.handover_assignment.batch.dispatched_at
            except (AttributeError, ObjectDoesNotExist):
                shipped_at = None
            defaults = {
                "source_batch_id": source.batch_id,
                "order": target_order,
                "reason_code": source.reason_code,
                "reason": source.reason,
                "planned_qty": source.planned_qty,
                "returned_qty": source.returned_qty,
                "created_by_id": source.created_by_id,
                "assigned_to_id": source.assigned_to_id,
                "shipped_at": shipped_at,
                "returned_at": source.completed_at,
                "source_created_at": source.created_at,
                "source_updated_at": source.updated_at,
                "last_synced_at": now,
                "source_snapshot": {
                    "marketplace_action": source.marketplace_action,
                    "marketplace_confirmed_at": (
                        source.marketplace_confirmed_at.isoformat()
                        if source.marketplace_confirmed_at
                        else ""
                    ),
                    "claimed_at": source.claimed_at.isoformat() if source.claimed_at else "",
                    "canceled_at": source.canceled_at.isoformat() if source.canceled_at else "",
                },
            }
            target = WmsNewReturn.objects.filter(source_request_id=source.id).first()
            if target is None:
                target = WmsNewReturn.objects.create(
                    source_request_id=source.id,
                    status=RETURN_STATUS_MAP.get(source.status, WmsNewReturn.STATUS_FAILED),
                    **defaults,
                )
                imported += 1
            else:
                for field, value in defaults.items():
                    setattr(target, field, value)
                if target.pilot_revision == 0:
                    target.status = RETURN_STATUS_MAP.get(
                        source.status, WmsNewReturn.STATUS_FAILED
                    )
                target.save()
                updated += 1
            linked += 1
    return {"imported": imported, "updated": updated, "linked": linked}


def _task_type(source: Task) -> str:
    route = str(source.route or "")
    if "/orders/receiving/" in route:
        return WmsNewTask.TYPE_ACCEPTANCE
    if "/orders/processing/" in route:
        return WmsNewTask.TYPE_PROCESSING
    if "/shipping/" in route:
        return WmsNewTask.TYPE_SHIPMENT
    return WmsNewTask.TYPE_OTHER


def _task_agency_id(source: Task, cache: dict[tuple[str, str], int | None]) -> int | None:
    route = str(source.route or "")
    match = re.search(r"/orders/(receiving|processing)/([^/]+)/", route)
    if match:
        order_type, order_id = match.groups()
        key = (order_type, order_id)
        if key not in cache:
            cache[key] = (
                OrderAuditEntry.objects.filter(order_type=order_type, order_id=order_id)
                .exclude(agency_id__isnull=True)
                .order_by("-created_at", "-id")
                .values_list("agency_id", flat=True)
                .first()
            )
        return cache[key]
    match = re.search(r"/shipping/(\d+)/", route)
    if match:
        order_id = match.group(1)
        key = ("shipping", order_id)
        if key not in cache:
            cache[key] = ShippingOrder.objects.filter(pk=int(order_id)).values_list(
                "agency_id", flat=True
            ).first()
        return cache[key]
    match = re.search(r"/logistics/(?:trips/)?(\d+)/", route)
    if match:
        trip_id = match.group(1)
        key = ("logistics", trip_id)
        if key not in cache:
            agency_ids = list(
                LogisticsTrip.objects.filter(pk=int(trip_id))
                .values_list("orders__shipping_order__agency_id", flat=True)
                .exclude(orders__shipping_order__agency_id__isnull=True)
                .distinct()[:2]
            )
            cache[key] = agency_ids[0] if len(agency_ids) == 1 else None
        return cache[key]
    return None


def sync_tasks(*, limit: int | None = None) -> dict[str, int]:
    queryset = Task.objects.select_related("assigned_to", "created_by").order_by("id")
    if limit:
        queryset = queryset[:limit]
    imported = 0
    updated = 0
    now = timezone.now()
    agency_cache: dict[tuple[str, str], int | None] = {}
    with transaction.atomic():
        for source in queryset:
            defaults = {
                "agency_id": _task_agency_id(source, agency_cache),
                "workflow_type": _task_type(source),
                "title": source.display_title(),
                "description": source.description,
                "assigned_to_id": source.assigned_to_id,
                "due_date": source.due_date,
                "source_updated_at": source.updated_at,
                "last_synced_at": now,
                "source_snapshot": {
                    "route": source.route,
                    "kind": source.kind,
                    "source_status": source.status,
                    "source_priority": source.priority,
                },
            }
            target = WmsNewTask.objects.filter(source_task_id=source.id).first()
            if target is None:
                WmsNewTask.objects.create(
                    source_task_id=source.id,
                    status=TASK_STATUS_MAP.get(source.status, WmsNewTask.STATUS_NEW),
                    priority=source.priority,
                    created_by=source.created_by,
                    **defaults,
                )
                imported += 1
            else:
                if target.pilot_revision == 0:
                    for field, value in defaults.items():
                        setattr(target, field, value)
                    target.status = TASK_STATUS_MAP.get(source.status, WmsNewTask.STATUS_NEW)
                    target.priority = source.priority
                else:
                    # The source task stays live for the old contour, while a pilot
                    # revision is an independent working copy in FBS-NEW.  Refresh
                    # only source metadata so the one-way sync cannot undo pilot work.
                    target.source_updated_at = source.updated_at
                    target.last_synced_at = now
                    snapshot = dict(target.source_snapshot or {})
                    snapshot.update(defaults["source_snapshot"])
                    target.source_snapshot = snapshot
                target.save()
                updated += 1
    return {"imported": imported, "updated": updated}


def _product_stock_maps() -> tuple[dict, dict, dict, dict]:
    warehouse_by_sku = defaultdict(
        lambda: {"qty": 0, "available": 0, "reserved": 0, "markings": 0}
    )
    warehouse_by_code = defaultdict(
        lambda: {"qty": 0, "available": 0, "reserved": 0, "markings": 0}
    )
    fbs_by_sku = defaultdict(lambda: {"qty": 0, "available": 0, "reserved": 0, "markings": 0})
    fbs_by_code = defaultdict(lambda: {"qty": 0, "available": 0, "reserved": 0, "markings": 0})

    warehouse_rows = (
        WarehouseStockSnapshot.objects.filter(is_archived=False, is_in_vehicle=False, qty__gt=0)
        .values("sku_ref_id", "agency_id", "sku_code")
        .annotate(
            qty=Sum("qty"),
            available=Sum("available_qty"),
            processing=Sum("processing_reserved_qty"),
            shipping=Sum("shipping_reserved_qty"),
            other=Sum("other_reserved_qty"),
            markings=Count("marking_code", distinct=True, filter=~Q(marking_code="")),
        )
    )
    for row in warehouse_rows:
        target = (
            warehouse_by_sku[int(row["sku_ref_id"])]
            if row["sku_ref_id"]
            else warehouse_by_code[(int(row["agency_id"]), str(row["sku_code"] or ""))]
        )
        target["qty"] += int(row["qty"] or 0)
        target["available"] += int(row["available"] or 0)
        target["reserved"] += int(row["processing"] or 0) + int(row["shipping"] or 0) + int(row["other"] or 0)
        target["markings"] += int(row["markings"] or 0)

    fbs_rows = (
        FbsStockBalance.objects.filter(qty__gt=0)
        .values("sku_ref_id", "agency_id", "sku_code")
        .annotate(
            qty=Sum("qty"),
            available=Sum("available_qty"),
            reserved=Sum("reserved_qty"),
            markings=Count("marking_code", distinct=True, filter=~Q(marking_code="")),
        )
    )
    for row in fbs_rows:
        target = (
            fbs_by_sku[int(row["sku_ref_id"])]
            if row["sku_ref_id"]
            else fbs_by_code[(int(row["agency_id"]), str(row["sku_code"] or ""))]
        )
        target["qty"] += int(row["qty"] or 0)
        target["available"] += int(row["available"] or 0)
        target["reserved"] += int(row["reserved"] or 0)
        target["markings"] += int(row["markings"] or 0)
    return warehouse_by_sku, warehouse_by_code, fbs_by_sku, fbs_by_code


def _decimal_or_zero(value) -> Decimal:
    return Decimal(value or 0)


def sync_products(*, limit: int | None = None) -> dict[str, int]:
    """Copy product cards and current stock totals without writing to legacy tables."""

    queryset = SKU.objects.select_related("agency").prefetch_related("barcodes").order_by("id")
    if limit:
        queryset = queryset[:limit]
    source_products = list(queryset)
    warehouse_by_sku, warehouse_by_code, fbs_by_sku, fbs_by_code = _product_stock_maps()
    imported = 0
    updated = 0
    now = timezone.now()

    with transaction.atomic():
        for source in source_products:
            if not source.agency_id:
                continue
            target = WmsNewProduct.objects.filter(source_sku_id=source.id).first()
            if source.deleted:
                if target and target.pilot_revision == 0 and not target.is_archived:
                    target.is_archived = True
                    target.last_synced_at = now
                    target.source_updated_at = source.updated_at
                    target.save(update_fields=("is_archived", "last_synced_at", "source_updated_at", "updated_at"))
                    updated += 1
                continue

            key = (int(source.agency_id), str(source.sku_code or ""))
            general = dict(warehouse_by_sku.get(source.id) or {})
            fbs = dict(fbs_by_sku.get(source.id) or {})
            for field in ("qty", "available", "reserved", "markings"):
                general[field] = int(general.get(field) or 0) + int(warehouse_by_code[key].get(field) or 0)
                fbs[field] = int(fbs.get(field) or 0) + int(fbs_by_code[key].get(field) or 0)
            primary_barcode = next(
                (item.value for item in source.barcodes.all() if item.is_primary),
                next((item.value for item in source.barcodes.all()), ""),
            )
            synced_values = {
                "agency_id": source.agency_id,
                "name": source.name,
                "article": source.sku_code,
                "barcode": primary_barcode,
                "image_url": source.img or "",
                "color": source.color or "",
                "size": source.size or "",
                "category": source.tovar_category or "",
                "weight_grams": _decimal_or_zero(source.weight_kg) * Decimal("1000"),
                "width_cm": _decimal_or_zero(source.width_mm) / Decimal("10"),
                "depth_cm": _decimal_or_zero(source.length_mm) / Decimal("10"),
                "height_cm": _decimal_or_zero(source.height_mm) / Decimal("10"),
                "marking_required": bool(source.honest_sign),
                "marking_count": int(general["markings"]) + int(fbs["markings"]),
                "stock_on_hand": int(general["qty"]) + int(fbs["qty"]),
                "stock_free": int(general["available"]) + int(fbs["available"]),
                "fbo_reserved": 0,
                "fbs_reserved": int(fbs["reserved"]),
                "internal_reserved": int(general["reserved"]),
                "description": source.description or "",
                "is_archived": False,
                "source_updated_at": source.updated_at,
                "last_synced_at": now,
                "source_snapshot": {
                    "source": source.source,
                    "brand": source.brand,
                    "market_id": source.market_id,
                    "store_id": source.stor_unit_id,
                    "goods_type": source.type_tovar or source.vid_tovar or "",
                },
            }
            if target is None:
                WmsNewProduct.objects.create(source_sku_id=source.id, **synced_values)
                imported += 1
                continue

            # Once a pilot user has changed a card, source sync only records source freshness.
            if target.pilot_revision:
                target.source_updated_at = source.updated_at
                target.last_synced_at = now
                target.source_snapshot = synced_values["source_snapshot"]
                target.save(update_fields=("source_updated_at", "last_synced_at", "source_snapshot", "updated_at"))
            else:
                for field, value in synced_values.items():
                    setattr(target, field, value)
                target.save()
            updated += 1
    return {"imported": imported, "updated": updated}


def _numeric_payload_value(payload, keys: tuple[str, ...]) -> int:
    if not isinstance(payload, dict):
        return 0
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float, Decimal)):
            return max(int(value), 0)
        if isinstance(value, str) and value.strip().replace(".", "", 1).isdigit():
            return max(int(float(value)), 0)
    for value in payload.values():
        if isinstance(value, dict):
            found = _numeric_payload_value(value, keys)
            if found:
                return found
    return 0


def _sum_payload_items(payload, keys: tuple[str, ...]) -> int:
    if not isinstance(payload, dict):
        return 0
    for list_key in ("items", "rows", "products", "accepted_items", "act_items"):
        rows = payload.get(list_key)
        if not isinstance(rows, list):
            continue
        total = 0
        for row in rows:
            if isinstance(row, dict):
                total += _numeric_payload_value(row, keys)
        if total:
            return total
    for value in payload.values():
        if isinstance(value, dict):
            total = _sum_payload_items(value, keys)
            if total:
                return total
    return 0


def _acceptance_state(latest: OrderAuditEntry) -> tuple[str, str]:
    payload = latest.payload if isinstance(latest.payload, dict) else {}
    source_status = _payload_value(payload, ("status_label", "status", "stage"))
    state_text = f"{source_status} {latest.description}".lower()
    if any(token in state_text for token in ("отмен", "cancel")):
        return WmsNewAcceptance.STATUS_CANCELLED, source_status
    if any(token in state_text for token in ("заверш", "выполн", "подтвержден", "принято складом", "done", "completed")):
        return WmsNewAcceptance.STATUS_DONE, source_status
    if any(token in state_text for token in ("работ", "приемк", "приёмк", "progress", "warehouse")):
        return WmsNewAcceptance.STATUS_IN_PROGRESS, source_status
    return WmsNewAcceptance.STATUS_CREATED, source_status


def sync_acceptances(*, limit: int | None = None) -> dict[str, int]:
    queryset = (
        OrderAuditEntry.objects.filter(order_type="receiving")
        .select_related("agency")
        .order_by("order_id", "created_at", "id")
    )
    grouped = defaultdict(list)
    for entry in queryset:
        grouped[str(entry.order_id)].append(entry)
    groups = list(grouped.items())
    if limit:
        groups = groups[:limit]
    imported = 0
    updated = 0
    now = timezone.now()
    with transaction.atomic():
        for order_key, entries in groups:
            latest = entries[-1]
            agency = next((entry.agency for entry in reversed(entries) if entry.agency_id), None)
            if agency is None:
                continue
            payload = latest.payload if isinstance(latest.payload, dict) else {}
            status, source_status = _acceptance_state(latest)
            received = _numeric_payload_value(
                payload,
                ("received_qty", "accepted_qty", "qty_received", "actual_qty", "qty_accepted"),
            ) or _sum_payload_items(
                payload,
                ("received_qty", "accepted_qty", "qty_received", "actual_qty", "qty_accepted", "qty"),
            )
            expected = _numeric_payload_value(
                payload,
                ("expected_qty", "planned_qty", "qty_expected", "requested_qty", "total_qty"),
            ) or _sum_payload_items(
                payload,
                ("expected_qty", "planned_qty", "qty_expected", "requested_qty", "quantity", "qty"),
            )
            mode = _payload_value(payload, ("receiving_mode", "acceptance_type", "mode")).lower()
            acceptance_type = (
                WmsNewAcceptance.TYPE_SCAN
                if any(token in mode for token in ("scan", "cz", "mark"))
                else WmsNewAcceptance.TYPE_MANUAL
            )
            defaults = {
                "agency": agency,
                "task_number": _payload_value(payload, ("task_number", "task_id")) or order_key,
                "title": _payload_value(payload, ("display_title", "title", "order_name", "comment"))
                or latest.description,
                "received_qty": received,
                "expected_qty": expected,
                "acceptance_type": acceptance_type,
                "status": status,
                "source_status": source_status,
                "source_created_at": entries[0].created_at,
                "completed_at": latest.created_at if status == WmsNewAcceptance.STATUS_DONE else None,
                "source_updated_at": latest.created_at,
                "last_synced_at": now,
                "source_snapshot": {
                    "latest_audit_id": latest.id,
                    "history_count": len(entries),
                    "latest_description": latest.description,
                    "payload": payload,
                },
            }
            target = WmsNewAcceptance.objects.filter(source_order_key=order_key).first()
            if target is None:
                WmsNewAcceptance.objects.create(source_order_key=order_key, **defaults)
                imported += 1
            elif target.pilot_revision:
                target.source_updated_at = latest.created_at
                target.last_synced_at = now
                target.source_snapshot = defaults["source_snapshot"]
                target.save(update_fields=("source_updated_at", "last_synced_at", "source_snapshot", "updated_at"))
                updated += 1
            else:
                for field, value in defaults.items():
                    setattr(target, field, value)
                target.save()
                updated += 1
    return {"imported": imported, "updated": updated}


def sync_marking_codes(*, limit: int | None = None) -> dict[str, int]:
    """Copy the legacy registry into WMS NEW without invoking legacy write services."""

    now = timezone.now()
    source_queryset = MarkingCode.objects.select_related("agency", "sku").order_by("id")
    max_source_id = (
        WmsNewMarkingCode.objects.exclude(source_code_id__isnull=True)
        .order_by("-source_code_id")
        .values_list("source_code_id", flat=True)
        .first()
    )
    if max_source_id:
        recent = now - timedelta(days=2)
        source_queryset = source_queryset.filter(
            Q(pk__gt=max_source_id)
            | Q(created_at__gte=recent)
            | Q(used_at__gte=recent)
            | Q(printed_at__gte=recent)
        )
    if limit:
        source_queryset = source_queryset[:limit]
    source_rows = list(source_queryset)
    if not source_rows:
        return {"imported": 0, "updated": 0}

    source_ids = [item.id for item in source_rows]
    source_codes = [item.code for item in source_rows]
    existing_by_source = {
        item.source_code_id: item
        for item in WmsNewMarkingCode.objects.filter(source_code_id__in=source_ids)
    }
    existing_by_code = {
        item.code: item
        for item in WmsNewMarkingCode.objects.filter(code__in=source_codes)
    }
    product_by_sku = {
        item.source_sku_id: item
        for item in WmsNewProduct.objects.filter(
            source_sku_id__in={row.sku_id for row in source_rows if row.sku_id}
        )
    }
    to_create = []
    to_update = []
    update_fields = (
        "source_code_id",
        "agency",
        "product",
        "product_name",
        "article",
        "barcode",
        "code_type",
        "source",
        "received_reference",
        "retired_reference",
        "received_at",
        "retired_at",
        "processed_at",
        "printed_at",
        "print_count",
        "source_updated_at",
        "last_synced_at",
        "source_snapshot",
        "updated_at",
    )
    for source in source_rows:
        product = product_by_sku.get(source.sku_id)
        source_updated_at = max(
            value for value in (source.created_at, source.used_at, source.printed_at) if value
        )
        values = {
            "source_code_id": source.id,
            "agency_id": source.agency_id,
            "product": product,
            "product_name": product.name if product else (source.sku.name if source.sku_id else ""),
            "article": product.article if product else source.sku_code,
            "barcode": source.barcode,
            "code_type": WmsNewMarkingCode.TYPE_UNIT,
            "source": (
                WmsNewMarkingCode.SOURCE_IMPORT
                if source.source == "import"
                else WmsNewMarkingCode.SOURCE_SCAN
            ),
            "received_reference": source.order_id if source.order_type == "receiving" else "",
            "retired_reference": source.order_id if source.used_at else "",
            "received_at": source.created_at,
            "retired_at": source.used_at,
            "processed_at": source.used_at if source.order_type == "processing" else None,
            "printed_at": source.printed_at,
            "print_count": 1 if source.printed_at else 0,
            "source_updated_at": source_updated_at,
            "last_synced_at": now,
            "source_snapshot": {
                "order_type": source.order_type,
                "order_id": source.order_id,
                "size": source.size,
                "box_barcode": source.box_barcode,
                "created_by_id": source.created_by_id,
                "used_by_id": source.used_by_id,
                "printed_by_id": source.printed_by_id,
            },
        }
        target = existing_by_source.get(source.id) or existing_by_code.get(source.code)
        if target is None:
            to_create.append(WmsNewMarkingCode(code=source.code, **values))
            continue
        if target.pilot_revision:
            if target.source_code_id is None:
                target.source_code_id = source.id
            target.source_updated_at = source_updated_at
            target.last_synced_at = now
            target.source_snapshot = values["source_snapshot"]
            target.updated_at = now
        else:
            for field, value in values.items():
                setattr(target, field, value)
            target.updated_at = now
        to_update.append(target)

    with transaction.atomic():
        if to_create:
            WmsNewMarkingCode.objects.bulk_create(to_create, batch_size=1000)
        if to_update:
            WmsNewMarkingCode.objects.bulk_update(to_update, update_fields, batch_size=1000)
    return {"imported": len(to_create), "updated": len(to_update)}


def sync_extra_fields(*, limit: int | None = None) -> dict[str, int]:
    now = timezone.now()
    source_definitions = GoodsExtraFieldDefinition.objects.order_by("id")
    if limit:
        source_definitions = source_definitions[:limit]
    source_definitions = list(source_definitions)
    imported = 0
    updated = 0
    definition_map = {}
    with transaction.atomic():
        for source in source_definitions:
            snapshot = {
                "name": source.name,
                "slug": source.slug,
                "field_type": source.field_type,
                "is_active": source.is_active,
                "sort_order": source.sort_order,
            }
            target = (
                WmsNewExtraFieldDefinition.objects.filter(source_definition_id=source.id).first()
                or WmsNewExtraFieldDefinition.objects.filter(code=source.slug).first()
            )
            if target is None:
                target = WmsNewExtraFieldDefinition.objects.create(
                    source_definition_id=source.id,
                    name=source.name,
                    code=source.slug,
                    field_type=source.field_type,
                    is_active=source.is_active,
                    sort_order=source.sort_order,
                    last_synced_at=now,
                    source_snapshot=snapshot,
                )
                imported += 1
            elif target.pilot_revision:
                if target.source_definition_id is None:
                    target.source_definition_id = source.id
                target.last_synced_at = now
                target.source_snapshot = snapshot
                target.save(
                    update_fields=(
                        "source_definition_id",
                        "last_synced_at",
                        "source_snapshot",
                        "updated_at",
                    )
                )
                updated += 1
            else:
                target.source_definition_id = source.id
                target.name = source.name
                target.code = source.slug
                target.field_type = source.field_type
                target.is_active = source.is_active
                target.sort_order = source.sort_order
                target.last_synced_at = now
                target.source_snapshot = snapshot
                target.save()
                updated += 1
            definition_map[source.id] = target

        source_values = GoodsExtraFieldValue.objects.select_related("sku", "definition").filter(
            definition_id__in=definition_map
        ).order_by("id")
        if limit:
            source_values = source_values[:limit]
        product_map = {
            item.source_sku_id: item
            for item in WmsNewProduct.objects.filter(
                source_sku_id__in=source_values.values_list("sku_id", flat=True)
            )
        }
        for source in source_values:
            definition = definition_map.get(source.definition_id)
            product = product_map.get(source.sku_id)
            if not definition or not product:
                continue
            snapshot = {
                "source_sku_id": source.sku_id,
                "source_definition_id": source.definition_id,
                "value": source.value,
                "updated_by_id": source.updated_by_id,
            }
            target = (
                WmsNewExtraFieldValue.objects.filter(source_value_id=source.id).first()
                or WmsNewExtraFieldValue.objects.filter(
                    definition=definition,
                    product=product,
                ).first()
            )
            if target is None:
                WmsNewExtraFieldValue.objects.create(
                    source_value_id=source.id,
                    definition=definition,
                    product=product,
                    value=source.value,
                    source_updated_at=source.updated_at,
                    last_synced_at=now,
                    source_snapshot=snapshot,
                )
                imported += 1
            elif target.pilot_revision:
                if target.source_value_id is None:
                    target.source_value_id = source.id
                target.source_updated_at = source.updated_at
                target.last_synced_at = now
                target.source_snapshot = snapshot
                target.save(
                    update_fields=(
                        "source_value_id",
                        "source_updated_at",
                        "last_synced_at",
                        "source_snapshot",
                        "updated_at",
                    )
                )
                updated += 1
            else:
                target.source_value_id = source.id
                target.definition = definition
                target.product = product
                target.value = source.value
                target.source_updated_at = source.updated_at
                target.last_synced_at = now
                target.source_snapshot = snapshot
                target.save()
                updated += 1
    return {"imported": imported, "updated": updated}


def sync_inventories(*, limit: int | None = None) -> dict[str, int]:
    """Mirror legacy inventory history without reusing its write services or locks."""

    queryset = FbsInventorySession.objects.select_related(
        "agency",
        "cell",
        "pallet",
        "box",
        "sku",
        "created_by",
        "first_counter",
        "second_counter",
        "approved_by",
    ).prefetch_related(
        "lines__balance__agency",
        "lines__balance__sku_ref",
        "lines__balance__box__pallet__cell__location",
        "lines__scans",
    ).order_by("id")
    if limit:
        queryset = queryset[:limit]
    source_sessions = list(queryset)
    if not source_sessions:
        return {"imported": 0, "updated": 0}

    from employees.models import Employee

    user_ids = {
        user_id
        for source in source_sessions
        for user_id in (
            source.first_counter_id,
            source.second_counter_id,
        )
        if user_id
    }
    for source in source_sessions:
        for line in source.lines.all():
            user_ids.update(
                scan.counted_by_id
                for scan in line.scans.all()
                if scan.counted_by_id
            )
    employee_by_user = {
        item.user_id: item
        for item in Employee.objects.filter(user_id__in=user_ids)
    }
    sku_ids = {source.sku_id for source in source_sessions if source.sku_id}
    for source in source_sessions:
        for line in source.lines.all():
            if line.balance.sku_ref_id:
                sku_ids.add(line.balance.sku_ref_id)
    product_by_sku = {
        item.source_sku_id: item
        for item in WmsNewProduct.objects.filter(source_sku_id__in=sku_ids)
    }
    imported = 0
    updated = 0
    now = timezone.now()
    status_map = {
        FbsInventorySession.STATUS_PLANNED: WmsNewInventorySession.STATUS_PLANNED,
        FbsInventorySession.STATUS_DRAINING: WmsNewInventorySession.STATUS_DRAINING,
        FbsInventorySession.STATUS_COUNTING: WmsNewInventorySession.STATUS_COUNTING,
        FbsInventorySession.STATUS_RECOUNT: WmsNewInventorySession.STATUS_RECOUNT,
        FbsInventorySession.STATUS_APPROVAL: WmsNewInventorySession.STATUS_APPROVAL,
        FbsInventorySession.STATUS_DONE: WmsNewInventorySession.STATUS_DONE,
        FbsInventorySession.STATUS_CANCELED: WmsNewInventorySession.STATUS_CANCELLED,
    }
    scope_map = {
        FbsInventorySession.SCOPE_ALL: WmsNewInventorySession.SCOPE_ALL,
        FbsInventorySession.SCOPE_AGENCY: WmsNewInventorySession.SCOPE_AGENCY,
        FbsInventorySession.SCOPE_CELL: WmsNewInventorySession.SCOPE_CELL,
        FbsInventorySession.SCOPE_PALLET: WmsNewInventorySession.SCOPE_PALLET,
        FbsInventorySession.SCOPE_BOX: WmsNewInventorySession.SCOPE_BOX,
        FbsInventorySession.SCOPE_SKU: WmsNewInventorySession.SCOPE_PRODUCT,
    }
    with transaction.atomic():
        for source in source_sessions:
            values = {
                "number": f"LEGACY-INV-{source.id}",
                "scope_type": scope_map[source.scope_type],
                "mode": source.mode,
                "scan_mode": source.scan_mode,
                "status": status_map[source.status],
                "agency_id": source.agency_id,
                "cell_id": source.cell_id,
                "pallet_id": source.pallet_id,
                "box_id": source.box_id,
                "product": product_by_sku.get(source.sku_id),
                "created_by_id": source.created_by_id,
                "first_counter": employee_by_user.get(source.first_counter_id),
                "second_counter": employee_by_user.get(source.second_counter_id),
                "approved_by_id": source.approved_by_id,
                "started_at": source.started_at,
                "completed_at": source.completed_at,
                "source_updated_at": source.updated_at,
                "last_synced_at": now,
                "source_snapshot": {
                    "source_created_at": source.created_at.isoformat(),
                    "legacy_status": source.status,
                    "legacy_scope_type": source.scope_type,
                },
            }
            target = WmsNewInventorySession.objects.filter(
                source_session_id=source.id
            ).first()
            if target is None:
                target = WmsNewInventorySession.objects.create(
                    source_session_id=source.id,
                    **values,
                )
                imported += 1
            elif target.pilot_revision:
                target.source_updated_at = source.updated_at
                target.last_synced_at = now
                target.source_snapshot = values["source_snapshot"]
                target.save(
                    update_fields=(
                        "source_updated_at",
                        "last_synced_at",
                        "source_snapshot",
                        "updated_at",
                    )
                )
                updated += 1
                continue
            else:
                for field, value in values.items():
                    setattr(target, field, value)
                target.save()
                updated += 1

            for source_line in source.lines.all():
                balance = source_line.balance
                line_values = {
                    "session": target,
                    "source_balance_id": balance.id,
                    "product": product_by_sku.get(balance.sku_ref_id),
                    "agency_id": balance.agency_id,
                    "location_code": balance.box.pallet.cell.warehouse_location_code,
                    "cell_code": balance.box.pallet.cell.cell_code,
                    "pallet_code": balance.box.pallet.pallet_code,
                    "box_code": balance.box.box_code,
                    "sku_code": balance.sku_code,
                    "product_name": balance.name,
                    "barcode": balance.barcode,
                    "marking_code": balance.marking_code,
                    "expected_qty": source_line.expected_qty,
                    "first_count_qty": source_line.first_count_qty,
                    "second_count_qty": source_line.second_count_qty,
                    "final_qty": source_line.final_qty,
                    "approved_delta": source_line.approved_delta,
                    "source_snapshot": {
                        "reserved_qty": int(balance.reserved_qty or 0),
                        "source_updated_at": balance.updated_at.isoformat(),
                    },
                }
                target_line = WmsNewInventoryLine.objects.filter(
                    source_line_id=source_line.id
                ).first()
                if target_line is None:
                    target_line = WmsNewInventoryLine.objects.create(
                        source_line_id=source_line.id,
                        **line_values,
                    )
                    imported += 1
                else:
                    for field, value in line_values.items():
                        setattr(target_line, field, value)
                    target_line.save()
                    updated += 1
                for source_scan in source_line.scans.all():
                    _scan, created = WmsNewInventoryScan.objects.update_or_create(
                        source_scan_id=source_scan.id,
                        defaults={
                            "line": target_line,
                            "count_round": source_scan.count_round,
                            "scan_code": source_scan.scan_code,
                            "qty": source_scan.qty,
                            "counted_by": employee_by_user.get(source_scan.counted_by_id),
                        },
                    )
                    if created:
                        imported += 1
                    else:
                        updated += 1
    return {"imported": imported, "updated": updated}


def sync_boxes(*, limit: int | None = None) -> dict[str, int]:
    """Copy physical boxes and their stock rows into the independent WMS NEW domain."""

    def differs(instance, field: str, value) -> bool:
        if field.endswith("_id"):
            return getattr(instance, field) != value
        if hasattr(value, "pk") and hasattr(instance, f"{field}_id"):
            return getattr(instance, f"{field}_id") != value.pk
        return getattr(instance, field) != value

    now = timezone.now()
    source_boxes_qs = WarehouseContainer.objects.filter(
        container_type=WarehouseContainer.TYPE_BOX
    ).select_related("agency", "parent_container", "current_location").order_by("id")
    if limit:
        source_boxes_qs = source_boxes_qs[:limit]
    source_boxes = list(source_boxes_qs)
    if not source_boxes:
        return {"imported": 0, "updated": 0}
    source_box_ids = [item.id for item in source_boxes]
    aggregates = {
        row["container_id"]: row
        for row in WarehouseStockSnapshot.objects.filter(
            container_id__in=source_box_ids,
            is_archived=False,
            is_in_vehicle=False,
        )
        .values("container_id")
        .annotate(
            sku_count=Count("sku_code", distinct=True),
            qty=Sum("qty"),
            available=Sum("available_qty"),
            processing=Sum("processing_reserved_qty"),
            shipping=Sum("shipping_reserved_qty"),
            other=Sum("other_reserved_qty"),
            markings=Count("marking_code", distinct=True, filter=~Q(marking_code="")),
        )
    }
    existing_boxes = {
        item.source_container_id: item
        for item in WmsNewBox.objects.filter(source_container_id__in=source_box_ids)
    }
    to_create = []
    to_update = []
    for source in source_boxes:
        totals = aggregates.get(source.id, {})
        values = {
            "agency_id": source.agency_id,
            "code": source.container_code,
            "source_parent_container_id": source.parent_container_id,
            "parent_code": (
                source.parent_container.container_code if source.parent_container_id else ""
            ),
            "location_id": source.current_location_id,
            "location_code": (
                source.current_location.location_code if source.current_location_id else ""
            ),
            "zone_code": (
                source.current_location.zone_code if source.current_location_id else ""
            ),
            "status": source.status,
            "gross_weight_g": source.gross_weight_g,
            "width_mm": source.width_mm,
            "height_mm": source.height_mm,
            "depth_mm": source.depth_mm,
            "sku_count": int(totals.get("sku_count") or 0),
            "stock_on_hand": int(totals.get("qty") or 0),
            "stock_free": int(totals.get("available") or 0),
            "reserved_qty": int(totals.get("processing") or 0)
            + int(totals.get("shipping") or 0)
            + int(totals.get("other") or 0),
            "marking_count": int(totals.get("markings") or 0),
            "source_context_type": source.source_context_type,
            "source_context_id": source.source_context_id,
            "source_updated_at": source.updated_at,
            "last_synced_at": now,
            "source_snapshot": {"source_created_at": source.created_at.isoformat()},
        }
        target = existing_boxes.get(source.id)
        if target is None:
            to_create.append(WmsNewBox(source_container_id=source.id, **values))
            continue
        compared_fields = (
            ("source_updated_at",)
            if target.pilot_revision
            else tuple(field for field in values if field != "last_synced_at")
        )
        if not any(differs(target, field, values[field]) for field in compared_fields):
            continue
        target.source_updated_at = source.updated_at
        target.last_synced_at = now
        if target.pilot_revision:
            snapshot = dict(target.source_snapshot or {})
            snapshot["latest_source"] = values["source_snapshot"]
            target.source_snapshot = snapshot
        else:
            for field, value in values.items():
                setattr(target, field, value)
        target.updated_at = now
        to_update.append(target)
    box_update_fields = (
        "agency",
        "code",
        "source_parent_container_id",
        "parent_code",
        "location",
        "location_code",
        "zone_code",
        "status",
        "gross_weight_g",
        "width_mm",
        "height_mm",
        "depth_mm",
        "sku_count",
        "stock_on_hand",
        "stock_free",
        "reserved_qty",
        "marking_count",
        "source_context_type",
        "source_context_id",
        "source_updated_at",
        "last_synced_at",
        "source_snapshot",
        "updated_at",
    )
    with transaction.atomic():
        if to_create:
            WmsNewBox.objects.bulk_create(to_create, batch_size=1000)
        if to_update:
            WmsNewBox.objects.bulk_update(to_update, box_update_fields, batch_size=1000)

    box_by_source = {
        item.source_container_id: item
        for item in WmsNewBox.objects.filter(source_container_id__in=source_box_ids)
    }
    source_items_qs = WarehouseStockSnapshot.objects.filter(
        container_id__in=source_box_ids,
        is_archived=False,
        is_in_vehicle=False,
    ).select_related("sku_ref").order_by("id")
    source_items = list(source_items_qs)
    source_item_ids = [item.id for item in source_items]
    existing_items = {
        item.source_snapshot_id: item
        for item in WmsNewBoxItem.objects.filter(source_snapshot_id__in=source_item_ids)
    }
    product_by_sku = {
        item.source_sku_id: item
        for item in WmsNewProduct.objects.filter(
            source_sku_id__in={row.sku_ref_id for row in source_items if row.sku_ref_id}
        )
    }
    item_create = []
    item_update = []
    for source in source_items:
        values = {
            "box": box_by_source[source.container_id],
            "product": product_by_sku.get(source.sku_ref_id),
            "agency_id": source.agency_id,
            "sku_code": source.sku_code,
            "product_name": source.name,
            "size": source.size,
            "barcode": source.barcode,
            "goods_type": source.goods_type,
            "marking_code": source.marking_code,
            "qty": int(source.qty or 0),
            "available_qty": int(source.available_qty or 0),
            "reserved_qty": int(source.processing_reserved_qty or 0)
            + int(source.shipping_reserved_qty or 0)
            + int(source.other_reserved_qty or 0),
            "warehouse_state_code": source.warehouse_state_code,
            "source_updated_at": source.updated_at,
            "last_synced_at": now,
            "source_snapshot": {
                "stock_unit_type": source.stock_unit_type,
                "source_context_type": source.source_context_type,
                "source_context_id": source.source_context_id,
                "snapshot_version": source.snapshot_version,
            },
        }
        target = existing_items.get(source.id)
        if target is None:
            item_create.append(WmsNewBoxItem(source_snapshot_id=source.id, **values))
            continue
        compared_fields = (
            ("source_updated_at",)
            if target.pilot_revision
            else tuple(field for field in values if field != "last_synced_at")
        )
        if not any(differs(target, field, values[field]) for field in compared_fields):
            continue
        target.source_updated_at = source.updated_at
        target.last_synced_at = now
        target.source_snapshot = values["source_snapshot"]
        if not target.pilot_revision:
            for field, value in values.items():
                setattr(target, field, value)
        target.updated_at = now
        item_update.append(target)
    item_update_fields = (
        "box",
        "product",
        "agency",
        "sku_code",
        "product_name",
        "size",
        "barcode",
        "goods_type",
        "marking_code",
        "qty",
        "available_qty",
        "reserved_qty",
        "warehouse_state_code",
        "source_updated_at",
        "last_synced_at",
        "source_snapshot",
        "updated_at",
    )
    with transaction.atomic():
        if item_create:
            WmsNewBoxItem.objects.bulk_create(item_create, batch_size=1000)
        if item_update:
            WmsNewBoxItem.objects.bulk_update(item_update, item_update_fields, batch_size=1000)
    return {
        "imported": len(to_create) + len(item_create),
        "updated": len(to_update) + len(item_update),
    }


MOVEMENT_RECEIPT_EVENTS = {
    "receiving_arrived",
    "placement_completed",
    "putaway_completed",
    "stock_returned_to_storage",
}
MOVEMENT_WRITEOFF_EVENTS = {
    "processing_consumed",
    "loaded_to_vehicle",
    "shipped",
    "stock_written_off",
}
MOVEMENT_ADJUSTMENT_EVENTS = {
    "stock_corrected",
    "inventory_adjusted",
    "stock_adjusted",
}
MOVEMENT_EVENT_LABELS = {
    "receiving_arrived": "Приход на приемку",
    "placement_completed": "Поступление и размещение товара",
    "putaway_completed": "Размещено на складе",
    "stock_returned_to_storage": "Возврат товара в остатки",
    "processing_consumed": "Списание в обработку",
    "loaded_to_vehicle": "Загружено в машину",
    "shipped": "Отгружено со склада",
    "stock_written_off": "Списание товара",
    "stock_corrected": "Изменение остатка",
    "inventory_adjusted": "Корректировка по инвентаризации",
    "stock_adjusted": "Изменение остатка",
    "movement_requested": "Задание на перемещение",
    "movement_started": "Перемещение начато",
    "movement_completed": "Перемещение внутри склада",
    "otg_requested": "Перемещение в зону отгрузки",
    "otg_arrived": "Перемещено в зону отгрузки",
}


def sync_movements(*, limit: int | None = None) -> dict[str, int]:
    """Copy recent legacy movement history without ever mutating its source events."""

    row_limit = limit if limit and limit > 0 else 5000
    source_events = list(
        WarehouseEvent.objects.filter(
            Q(from_location__isnull=False)
            | Q(to_location__isnull=False)
            | Q(event_type__in=(
                MOVEMENT_RECEIPT_EVENTS
                | MOVEMENT_WRITEOFF_EVENTS
                | MOVEMENT_ADJUSTMENT_EVENTS
            ))
        )
        .select_related("agency", "container", "from_location", "to_location", "performed_by")
        .order_by("-occurred_at", "-id")[:row_limit]
    )
    if not source_events:
        return {"imported": 0, "updated": 0}
    source_ids = [event.id for event in source_events]
    existing = {
        item.source_event_id: item
        for item in WmsNewMovement.objects.filter(source_event_id__in=source_ids)
    }
    snapshot_ids = set()
    container_ids = set()
    container_codes = set()
    event_container_codes = {}
    for event in source_events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.container_id:
            container_ids.add(event.container_id)
        codes = []
        for key in ("box_code", "source_box_code"):
            if payload.get(key):
                codes.append(str(payload[key]))
        for key in ("container_codes", "box_codes"):
            values = payload.get(key)
            if isinstance(values, (list, tuple)):
                codes.extend(str(value) for value in values if value)
        event_container_codes[event.id] = codes
        container_codes.update(codes)
        for key in ("snapshot_id", "source_snapshot_id"):
            try:
                value = int(payload.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value:
                snapshot_ids.add(value)
    containers_by_code = {
        item.container_code: item
        for item in WarehouseContainer.objects.filter(container_code__in=container_codes)
    }
    container_ids.update(item.id for item in containers_by_code.values())
    source_snapshots = list(
        WarehouseStockSnapshot.objects.filter(
            Q(id__in=snapshot_ids) | Q(container_id__in=container_ids),
            is_archived=False,
        ).select_related("sku_ref")
    )
    snapshot_by_id = {
        item.id: item
        for item in source_snapshots
    }
    snapshots_by_container = defaultdict(list)
    for item in source_snapshots:
        if item.container_id:
            snapshots_by_container[item.container_id].append(item)
    product_by_sku = {
        item.source_sku_id: item
        for item in WmsNewProduct.objects.filter(
            source_sku_id__in={
                snapshot.sku_ref_id for snapshot in source_snapshots if snapshot.sku_ref_id
            }
        )
    }
    create_rows = []
    update_rows = []
    for event in source_events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        snapshot = None
        for key in ("snapshot_id", "source_snapshot_id"):
            try:
                snapshot = snapshot_by_id.get(int(payload.get(key) or 0))
            except (TypeError, ValueError):
                snapshot = None
            if snapshot is not None:
                break
        candidates = []
        if event.container_id:
            candidates.extend(snapshots_by_container.get(event.container_id, ()))
        for code in event_container_codes.get(event.id, ()):
            container = containers_by_code.get(code)
            if container is not None:
                candidates.extend(snapshots_by_container.get(container.id, ()))
        if snapshot is None and candidates:
            matching_qty = [item for item in candidates if int(item.qty or 0) == int(event.qty or 0)]
            distinct_skus = {item.sku_ref_id or item.sku_code for item in candidates}
            if len(matching_qty) == 1:
                snapshot = matching_qty[0]
            elif len(distinct_skus) == 1:
                snapshot = candidates[0]
        product = product_by_sku.get(snapshot.sku_ref_id) if snapshot else None
        product_name = str(payload.get("name") or (snapshot.name if snapshot else "") or "")
        article = str(payload.get("sku_code") or (snapshot.sku_code if snapshot else "") or "")
        if not product_name and candidates:
            distinct_names = {item.name for item in candidates if item.name}
            if len(distinct_names) == 1:
                product_name = next(iter(distinct_names))
            elif event.container_id and event.container:
                product_name = f"Содержимое короба {event.container.container_code}"
            else:
                product_name = f"Содержимое {len({item.container_id for item in candidates})} коробов"
        if not article and candidates:
            articles = sorted({item.sku_code for item in candidates if item.sku_code})
            article = ", ".join(articles[:3])
            if len(articles) > 3:
                article += "…"
        if event.event_type in MOVEMENT_RECEIPT_EVENTS:
            action = WmsNewMovement.ACTION_RECEIPT
        elif event.event_type in MOVEMENT_WRITEOFF_EVENTS:
            action = WmsNewMovement.ACTION_WRITEOFF
        elif event.event_type in MOVEMENT_ADJUSTMENT_EVENTS:
            action = WmsNewMovement.ACTION_ADJUSTMENT
        else:
            action = WmsNewMovement.ACTION_MOVEMENT
        quantity = int(event.qty or 0)
        if action == WmsNewMovement.ACTION_WRITEOFF:
            quantity = -quantity
        elif action == WmsNewMovement.ACTION_ADJUSTMENT:
            try:
                quantity = int(payload.get("qty_delta", quantity) or 0)
            except (TypeError, ValueError):
                pass
        try:
            balance_after = int(payload.get("balance_after") or 0)
        except (TypeError, ValueError):
            balance_after = 0
        if not balance_after and snapshot is not None:
            balance_after = int(snapshot.qty or 0)
        actor_name = ""
        if event.performed_by_id:
            actor_name = str(event.performed_by.get_full_name() or "").strip()
            actor_name = actor_name or str(event.performed_by.get_username())
        document = ""
        if event.source_document_type or event.source_document_id:
            document = " · ".join(
                value for value in (event.source_document_type, event.source_document_id) if value
            )
        information = MOVEMENT_EVENT_LABELS.get(
            event.event_type, event.event_type.replace("_", " ")
        )
        if document:
            information = f"{information} · {document}"
        values = {
            "agency": event.agency,
            "product": product,
            "product_name": product.name if product else product_name,
            "article": product.article if product else article,
            "action": action,
            "source_location": event.from_location,
            "source_location_name": (
                str(event.from_location) if event.from_location_id else event.from_zone_code
            ),
            "target_location": event.to_location,
            "target_location_name": (
                str(event.to_location) if event.to_location_id else event.to_zone_code
            ),
            "quantity": quantity,
            "balance_after": balance_after,
            "information": information,
            "source_event_type": event.event_type,
            "actor": event.performed_by,
            "actor_name": actor_name,
            "occurred_at": event.occurred_at,
            "payload": payload,
        }
        target = existing.get(event.id)
        if target is None:
            create_rows.append(
                WmsNewMovement(
                source_event_id=event.id,
                    **values,
                )
            )
            continue
        enriched = False
        for field in ("product", "product_name", "article", "balance_after"):
            value = values[field]
            current = getattr(target, f"{field}_id") if field == "product" else getattr(target, field)
            expected = value.id if field == "product" and value is not None else value
            if current != expected and (field == "balance_after" or bool(expected)):
                setattr(target, field, value)
                enriched = True
        if enriched:
            update_rows.append(target)
    if create_rows:
        WmsNewMovement.objects.bulk_create(create_rows, batch_size=1000, ignore_conflicts=True)
    if update_rows:
        WmsNewMovement.objects.bulk_update(
            update_rows,
            ("product", "product_name", "article", "balance_after"),
            batch_size=1000,
        )
    return {"imported": len(create_rows), "updated": len(update_rows)}


LOGISTICS_ROUTE_SERVICES = (
    "OZON FBS",
    "OZON Real FBS",
    "Wildberries FBS",
    "YandexMarket DBS",
    "YandexMarket FBS",
    "Курьер",
)


def _shipping_logistics_status(source: ShippingOrder, weight_g: int) -> str:
    if source.status == ShippingOrder.STATUS_CANCELED:
        return WmsNewLogisticsOrder.STATUS_CARRIER_CANCELLED
    if weight_g <= 0:
        return WmsNewLogisticsOrder.STATUS_UNMEASURED
    return WmsNewLogisticsOrder.STATUS_NO_DIMENSIONS


def _manifest_status(source: LogisticsTrip) -> str:
    if source.status == LogisticsTrip.STATUS_CANCELED:
        return WmsNewLogisticsManifest.STATUS_CANCELLED
    if source.status == LogisticsTrip.STATUS_COMPLETED:
        return WmsNewLogisticsManifest.STATUS_COMPLETED
    if source.status == LogisticsTrip.STATUS_DEPARTED:
        return WmsNewLogisticsManifest.STATUS_SENT
    return WmsNewLogisticsManifest.STATUS_NEW


def sync_logistics(*, limit: int | None = None) -> dict[str, int]:
    """Copy ShippingOrder and LogisticsTrip into the isolated donor-shaped domain."""

    now = timezone.now()
    imported = 0
    updated = 0
    source_orders_qs = (
        ShippingOrder.objects.select_related("agency", "marketplace", "transport_note")
        .prefetch_related("items")
        .order_by("-created_at", "-id")
    )
    if limit:
        source_orders_qs = source_orders_qs[:limit]
    source_orders = list(source_orders_qs)
    existing_orders = {
        item.source_shipping_order_id: item
        for item in WmsNewLogisticsOrder.objects.filter(
            source_shipping_order_id__in=[source.id for source in source_orders]
        )
    }
    for source in source_orders:
        note = None
        try:
            note = source.transport_note
        except ObjectDoesNotExist:
            pass
        weight_g = int((note.cargo_weight_kg or 0) * 1000) if note else 0
        source_label = str(source.marketplace or source.get_delivery_type_display())
        tracking = source.shipping_barcode or source.wb_supply_barcode or ""
        values = {
            "agency": source.agency,
            "number": source.number,
            "tracking_number": tracking,
            "source_type": WmsNewLogisticsOrder.SOURCE_SHIPPING,
            "source_label": source_label,
            "weight_g": weight_g,
            "item_count": sum(item.qty_requested for item in source.items.all()),
            "status": _shipping_logistics_status(source, weight_g),
            "delivery_status": source.get_status_display(),
            "source_updated_at": source.updated_at,
            "last_checked_at": source.updated_at,
            "last_synced_at": now,
            "source_snapshot": {
                "delivery_type": source.delivery_type,
                "marketplace_id": source.marketplace_id,
                "destination_warehouse": source.destination_warehouse,
                "planned_ship_date": (
                    source.planned_ship_date.isoformat() if source.planned_ship_date else None
                ),
            },
            "created_at": source.created_at,
        }
        target = existing_orders.get(source.id)
        if target is None:
            target = WmsNewLogisticsOrder.objects.create(
                source_shipping_order_id=source.id,
                **{key: value for key, value in values.items() if key != "created_at"},
            )
            WmsNewLogisticsOrder.objects.filter(pk=target.pk).update(
                created_at=source.created_at
            )
            imported += 1
            continue
        sync_fields = (
            "agency",
            "number",
            "tracking_number",
            "source_type",
            "source_label",
            "item_count",
            "delivery_status",
            "source_updated_at",
            "last_checked_at",
            "last_synced_at",
            "source_snapshot",
            "created_at",
        )
        if not target.pilot_revision:
            sync_fields += ("weight_g", "status")
        changed = False
        for field in sync_fields:
            value = values[field]
            current = getattr(target, f"{field}_id") if hasattr(value, "pk") else getattr(target, field)
            expected = value.pk if hasattr(value, "pk") else value
            if current != expected:
                setattr(target, field, value)
                changed = True
        if changed:
            target.save(update_fields=sync_fields + ("updated_at",))
            updated += 1

    source_trips_qs = (
        LogisticsTrip.objects.select_related("carrier")
        .prefetch_related("orders__shipping_order__items", "orders__shipping_order__transport_note")
        .order_by("-created_at", "-id")
    )
    if limit:
        source_trips_qs = source_trips_qs[:limit]
    source_trips = list(source_trips_qs)
    existing_manifests = {
        item.source_trip_id: item
        for item in WmsNewLogisticsManifest.objects.filter(
            source_trip_id__in=[source.id for source in source_trips]
        )
    }
    for source in source_trips:
        total_items = 0
        total_weight_g = 0
        for link in source.orders.all():
            total_items += sum(item.qty_requested for item in link.shipping_order.items.all())
            try:
                note = link.shipping_order.transport_note
            except ObjectDoesNotExist:
                note = None
            if note and note.cargo_weight_kg:
                total_weight_g += int(note.cargo_weight_kg * 1000)
        values = {
            "name": source.number,
            "status": _manifest_status(source),
            "logistics_company": str(source.carrier or ""),
            "total_items": total_items,
            "total_weight_g": total_weight_g,
            "expected_arrival_date": source.trip_date,
            "source_updated_at": source.updated_at,
            "last_synced_at": now,
            "source_snapshot": {
                "trip_kind": source.trip_kind,
                "driver": source.driver_name,
                "vehicle": source.vehicle_number or source.vehicle_name,
                "source_status": source.status,
                "linked_orders": source.orders.count(),
            },
            "created_at": source.created_at,
        }
        target = existing_manifests.get(source.id)
        if target is None:
            target = WmsNewLogisticsManifest.objects.create(
                source_trip_id=source.id,
                **{key: value for key, value in values.items() if key != "created_at"},
            )
            WmsNewLogisticsManifest.objects.filter(pk=target.pk).update(
                created_at=source.created_at
            )
            imported += 1
            continue
        sync_fields = (
            "logistics_company",
            "total_items",
            "total_weight_g",
            "source_updated_at",
            "last_synced_at",
            "source_snapshot",
            "created_at",
        )
        if not target.pilot_revision:
            sync_fields += ("name", "status", "expected_arrival_date")
        changed = False
        for field in sync_fields:
            if getattr(target, field) != values[field]:
                setattr(target, field, values[field])
                changed = True
        if changed:
            target.save(update_fields=sync_fields + ("updated_at",))
            updated += 1

    for service in LOGISTICS_ROUTE_SERVICES:
        _rule, created = WmsNewLogisticsRouteRule.objects.get_or_create(
            delivery_service=service
        )
        imported += int(created)
    return {"imported": imported, "updated": updated}


def _acceptance_document_lines(source: WmsNewAcceptance) -> list[dict]:
    snapshot = source.source_snapshot if isinstance(source.source_snapshot, dict) else {}
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    flow = payload.get("flow_state") if isinstance(payload.get("flow_state"), dict) else {}
    grouped: dict[tuple[str, str, str], dict] = {}
    for container_key in ("boxes", "pallets"):
        containers = flow.get(container_key)
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, dict):
                continue
            for row in container.get("items") or ():
                if not isinstance(row, dict):
                    continue
                article = str(row.get("sku_code") or row.get("sku") or "").strip()
                name = str(row.get("name") or article or "Товар").strip()
                barcode = str(row.get("barcode") or "").strip()
                key = (article, name, barcode)
                item = grouped.setdefault(
                    key,
                    {
                        "article": article,
                        "product_name": name,
                        "barcode": barcode,
                        "quantity": 0,
                        "location_name": str(container.get("code") or ""),
                        "source_snapshot": {"container": container.get("code") or ""},
                    },
                )
                item["quantity"] += max(int(row.get("qty") or 0), 0)
    return [row for row in grouped.values() if row["quantity"] > 0]


def _shipment_document_lines(source: WmsNewShipment) -> list[dict]:
    grouped: dict[tuple[str, str, str], dict] = {}
    links = source.shipment_orders.select_related("order").prefetch_related("order__items")
    for link in links:
        for row in link.order.items.all():
            article = str(row.external_sku or (row.sku.sku_code if row.sku_id else "") or "").strip()
            name = str(row.product_name or article or "Товар").strip()
            barcode = str(row.barcode or "").strip()
            key = (article, name, barcode)
            item = grouped.setdefault(
                key,
                {
                    "article": article,
                    "product_name": name,
                    "barcode": barcode,
                    "quantity": 0,
                    "location_name": "Стол отгрузки",
                    "source_snapshot": {"orders": []},
                },
            )
            item["quantity"] += max(int(row.quantity or 0), 0)
            item["source_snapshot"]["orders"].append(link.order.external_order_id)
    return [row for row in grouped.values() if row["quantity"] > 0]


def _sync_document(
    *,
    source_key: str,
    agency_id: int,
    document_type: str,
    status: str,
    basis: str,
    source_created_at,
    source_updated_at,
    posted_at,
    source_snapshot: dict,
    lines: list[dict],
    source_acceptance_id: int | None = None,
    source_shipment_id: int | None = None,
) -> tuple[WmsNewDocument, bool]:
    now = timezone.now()
    target = WmsNewDocument.objects.filter(source_key=source_key).first()
    created = target is None
    values = {
        "agency_id": agency_id,
        "document_type": document_type,
        "status": status,
        "basis": basis,
        "source_created_at": source_created_at,
        "source_updated_at": source_updated_at,
        "last_synced_at": now,
        "posted_at": posted_at,
        "source_snapshot": source_snapshot,
        "source_acceptance_id": source_acceptance_id,
        "source_shipment_id": source_shipment_id,
    }
    if target is None:
        target = WmsNewDocument.objects.create(source_key=source_key, **values)
    elif target.pilot_revision:
        target.source_updated_at = source_updated_at
        target.last_synced_at = now
        target.source_snapshot = source_snapshot
        target.save(update_fields=("source_updated_at", "last_synced_at", "source_snapshot", "updated_at"))
        return target, False
    else:
        for field, value in values.items():
            setattr(target, field, value)
        target.save()

    product_map = {
        item.article: item
        for item in WmsNewProduct.objects.filter(
            agency_id=agency_id,
            article__in={row["article"] for row in lines if row["article"]},
            is_archived=False,
        )
    }
    target.items.all().delete()
    WmsNewDocumentItem.objects.bulk_create(
        [
            WmsNewDocumentItem(
                document=target,
                product=product_map.get(row["article"]),
                product_name=row["product_name"],
                article=row["article"],
                barcode=row["barcode"],
                location_name=row["location_name"],
                quantity=row["quantity"],
                source_line_key=f"{source_key}:{index}",
                source_snapshot=row["source_snapshot"],
            )
            for index, row in enumerate(lines, start=1)
        ],
        batch_size=500,
    )
    target.sku_count = len(lines)
    target.unit_count = sum(int(row["quantity"] or 0) for row in lines)
    target.save(update_fields=("sku_count", "unit_count", "updated_at"))
    return target, created


def sync_documents(*, limit: int | None = None) -> dict[str, int]:
    imported = 0
    updated = 0
    acceptances = WmsNewAcceptance.objects.select_related("agency").order_by("id")
    shipments = WmsNewShipment.objects.select_related("agency").order_by("id")
    if limit:
        acceptances = acceptances[:limit]
        shipments = shipments[:limit]
    with transaction.atomic():
        for source in acceptances:
            status = {
                WmsNewAcceptance.STATUS_DONE: WmsNewDocument.STATUS_POSTED,
                WmsNewAcceptance.STATUS_CANCELLED: WmsNewDocument.STATUS_CANCELLED,
            }.get(source.status, WmsNewDocument.STATUS_DRAFT)
            _, created = _sync_document(
                source_key=f"acceptance:{source.id}",
                agency_id=source.agency_id,
                document_type=WmsNewDocument.TYPE_RECEIPT,
                status=status,
                basis=f"Приемка №{source.task_number or source.source_order_key}",
                source_created_at=source.source_created_at or source.created_at,
                source_updated_at=source.source_updated_at or source.updated_at,
                posted_at=source.completed_at if status == WmsNewDocument.STATUS_POSTED else None,
                source_snapshot={"acceptance_id": source.id, "source_order_key": source.source_order_key},
                lines=_acceptance_document_lines(source),
                source_acceptance_id=source.id,
            )
            imported += int(created)
            updated += int(not created)
        for source in shipments:
            posted = bool(source.dispatched_at) or source.status in {
                WmsNewShipment.STATUS_IN_TRANSIT,
                WmsNewShipment.STATUS_ACCEPTED,
                WmsNewShipment.STATUS_REJECTED,
                WmsNewShipment.STATUS_PARTIAL,
            }
            cancelled = source.status == WmsNewShipment.STATUS_REJECTED and not posted
            status = (
                WmsNewDocument.STATUS_CANCELLED
                if cancelled
                else WmsNewDocument.STATUS_POSTED
                if posted
                else WmsNewDocument.STATUS_DRAFT
            )
            label = source.external_supply_id or source.external_name or source.source_batch_id or source.id
            _, created = _sync_document(
                source_key=f"shipment:{source.id}",
                agency_id=source.agency_id,
                document_type=WmsNewDocument.TYPE_WRITEOFF,
                status=status,
                basis=f"Отгрузка (FBS) №{label}",
                source_created_at=source.source_created_at or source.created_at,
                source_updated_at=source.source_updated_at or source.updated_at,
                posted_at=source.dispatched_at if status == WmsNewDocument.STATUS_POSTED else None,
                source_snapshot={"shipment_id": source.id, "source_batch_id": source.source_batch_id},
                lines=_shipment_document_lines(source),
                source_shipment_id=source.id,
            )
            imported += int(created)
            updated += int(not created)
    return {"imported": imported, "updated": updated}


def _run(scope: str, callback) -> dict[str, int]:
    run = WmsNewSyncRun.objects.create(scope=scope)
    try:
        result = callback()
    except Exception as exc:
        run.status = WmsNewSyncRun.STATUS_FAILED
        run.error = str(exc)[:4000]
        run.finished_at = timezone.now()
        run.save(update_fields=("status", "error", "finished_at"))
        raise
    run.status = WmsNewSyncRun.STATUS_DONE
    run.imported = int(result.get("imported") or 0)
    run.updated = int(result.get("updated") or 0)
    run.finished_at = timezone.now()
    run.save(update_fields=("status", "imported", "updated", "finished_at"))
    return result


def sync_all() -> dict[str, dict[str, int]]:
    return {
        "orders": _run("orders", sync_orders),
        "waves": _run("waves", sync_waves),
        "shipments": _run("shipments", sync_shipments),
        "returns": _run("returns", sync_returns),
        "tasks": _run("tasks", sync_tasks),
        "products": _run("products", sync_products),
        "acceptances": _run("acceptances", sync_acceptances),
        "marking": _run("marking", sync_marking_codes),
        "extra_fields": _run("extra_fields", sync_extra_fields),
        "inventories": _run("inventories", sync_inventories),
        "boxes": _run("boxes", sync_boxes),
        "movements": _run("movements", sync_movements),
        "logistics": _run("logistics", sync_logistics),
        "documents": _run("documents", sync_documents),
    }
