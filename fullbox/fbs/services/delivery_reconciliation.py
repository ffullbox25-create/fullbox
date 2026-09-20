"""Reconcile marketplace delivery without deducting already picked stock twice.

Stock is consumed atomically by picking._complete_pick_allocation_once. A
marketplace confirmation is not permission to choose replacement stock, undo
returns or manufacture physical handover scans. Incomplete evidence is blocked.
"""
from collections import defaultdict
from datetime import datetime
import hashlib
import json

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from fbs.flags import feature_enabled
from fbs.models import (
    FbsOrder, FbsOrderItem, FbsOrderStockAllocation, FbsPickRestockLine,
    FbsPickRestockRequest, FbsPickTask, FbsProblemToteItem, FbsToteMovement,
    FbsOrderTraceability, FbsPickException, FbsPickVerificationProgress,
    FbsHandoverOrder,
)
from fbs.order_audit import log_order_snapshot


RECONCILABLE_STATES = ("picked", "ready_for_handover", "handed_over", "exception")


def is_delivered(marketplace, status, substatus=""):
    status = str(status or "").strip().lower()
    substatus = str(substatus or "").strip().lower()
    return (marketplace == "ozon" and status == "delivered") or (
        marketplace == "wb" and status == "complete" and substatus == "sold"
    )


def delivered_orders_q():
    return Q(profile__marketplace="ozon", marketplace_status__iexact="delivered") | Q(
        profile__marketplace="wb", marketplace_status__iexact="complete",
        marketplace_substatus__iexact="sold",
    )


def _quarantine_evidence(order, allocations, *, lock=False):
    """Separate a physically retained invalid-KIZ unit from its shipped replacement.

    Never infer quarantine from a task status or free-text reason alone. Require
    the immutable placement event, exact serial/allocation, and still-present
    problem item. This function only reads; it does not resolve the problem.
    """
    def rows(qs):
        return list(qs.select_for_update(of=("self",)) if lock else qs)

    problems = rows(FbsProblemToteItem.objects.filter(
        order=order, status="in_tote", severity="critical").order_by("id"))
    if not problems:
        return [], None
    by_id = {a.pk: a for a in allocations}
    tasks = {t.pk: t for t in rows(FbsPickTask.objects.filter(
        id__in=[a.pick_task_id for a in allocations if a.pick_task_id]).order_by("id"))}
    traces = {t.allocation_id: t for t in rows(FbsOrderTraceability.objects.filter(
        allocation__in=allocations).order_by("id"))}
    issues = rows(FbsPickException.objects.filter(
        allocation__in=allocations, status="open").order_by("id"))
    excluded = []
    for problem in problems:
        movements = rows(FbsToteMovement.objects.filter(
            tote_id=problem.problem_tote_id, controller_session_id=problem.session_id,
            action="place", target_kind="problem_tote",
            details__problem_item_id=problem.pk,
        ).order_by("id"))
        if len(movements) != 1:
            return [], "quarantine_evidence_incomplete"
        m = movements[0]
        d = m.details if isinstance(m.details, dict) else {}
        # JSON identifiers must be exact integers, not a truthy string or bool.
        if any(type(d.get(key)) is not int for key in (
                "allocation_id", "order_id", "order_item_id", "source_balance_id")):
            return [], "quarantine_evidence_incomplete"
        a = by_id.get(d["allocation_id"])
        task = tasks.get(a.pick_task_id) if a else None
        trace = traces.get(a.pk) if a else None
        if not (a and task and trace and trace.marking_code and
                trace.status == "picked" and trace.qty == a.qty_picked and
                a.qty_picked == problem.quantity == m.quantity and a.qty_picked > 0 and
                a.status == "picked" and task.status == "exception" and
                task.order_id == order.pk and m.pick_batch_id == task.batch_id and
                a.order_item_id == problem.order_item_id and
                d.get("order_id") == order.pk and
                d.get("order_item_id") == a.order_item_id and
                d.get("source_balance_id") == a.balance_id and
                d.get("physically_confirmed") is True and d.get("route") == "rewave" and
                d.get("scan") == problem.scanned_value == trace.marking_code and
                a.picked_at and a.picked_at <= m.created_at and
                any(i.allocation_id == a.pk and i.task_id == task.pk for i in issues)):
            return [], "quarantine_evidence_incomplete"
        if any(e["allocation_id"] == a.pk for e in excluded):
            return [], "quarantine_evidence_incomplete"
        excluded.append({"allocation_id": a.pk, "quantity": a.qty_picked,
                         "problem_item_id": problem.pk, "movement_id": m.pk,
                         "placed_at": m.created_at.isoformat()})

    excluded_ids = {e["allocation_id"] for e in excluded}
    replacements = [a for a in allocations if a.qty_picked and a.pk not in excluded_ids]
    verification = {v.allocation_id: v for v in rows(FbsPickVerificationProgress.objects.filter(
        allocation__in=replacements).order_by("id"))}
    latest_placement = max(datetime.fromisoformat(e["placed_at"]) for e in excluded)
    old_marks = {traces[e["allocation_id"]].marking_code for e in excluded}
    for a in replacements:
        task, trace, verified = tasks.get(a.pick_task_id), traces.get(a.pk), verification.get(a.pk)
        if not (task and task.status == "picked" and a.picked_at and
                a.picked_at > latest_placement and trace and trace.marking_code and
                trace.status == "picked" and trace.qty == a.qty_picked and
                trace.marking_code not in old_marks and verified and verified.completed_at and
                verified.qty_verified == a.qty_picked):
            return [], "replacement_delivery_unproven"
    if not replacements:
        return [], "replacement_delivery_unproven"
    handed = FbsHandoverOrder.objects.filter(
        order=order, status="active", box__batch__profile_id=order.profile_id,
        box__batch__status__in=("dispatched", "accepted"),
        box__batch__dispatched_at__gte=max(a.picked_at for a in replacements),
    )
    if not rows(handed.order_by("id")):
        return [], "replacement_delivery_unproven"
    return excluded, None


def _decision(order, *, lock=False):
    result = {"order_id": order.pk, "external_order_id": order.external_order_id,
              "previous_status": order.internal_status, "action": "blocked",
              "reason": "", "already_debited_qty": 0, "debit_qty": 0}
    def blocked(reason):
        return {**result, "reason": reason}
    if not is_delivered(order.profile.marketplace, order.marketplace_status, order.marketplace_substatus):
        return blocked("marketplace_not_delivered")
    if order.internal_status == FbsOrder.STATUS_DELIVERED:
        return {**result, "action": "unchanged", "reason": "already_delivered"}
    if order.internal_status not in RECONCILABLE_STATES:
        return blocked("local_status_excluded")
    items = FbsOrderItem.objects.filter(order=order).order_by("id")
    allocations = FbsOrderStockAllocation.objects.filter(order_item__order=order).order_by("id")
    if lock:
        items = items.select_for_update()
        allocations = allocations.select_for_update(of=("self",))
    items = list(items)
    allocations = list(allocations.select_related("balance", "order_item__sku"))
    if not items or not allocations:
        return blocked("no_source_allocations")
    # Include wave-wide returns through their lines; canceled requests with an
    # actual returned quantity also remain blocked. Never undo a physical return.
    returns = FbsPickRestockLine.objects.filter(allocation__in=allocations).filter(
        ~Q(status="canceled") | Q(returned_qty__gt=0)
    )
    requests = FbsPickRestockRequest.objects.filter(order=order).filter(
        ~Q(status="canceled") | Q(returned_qty__gt=0)
    )
    if returns.exists() or requests.exists():
        return blocked("return_conflict")
    if FbsPickTask.objects.filter(order=order, status__in=("queued", "in_progress")).exists():
        return blocked("active_pick_task")
    picked = defaultdict(int)
    for a in allocations:
        if a.balance.agency_id != order.profile.agency_id:
            return blocked("client_mismatch")
        if a.order_item.sku_id and a.balance.sku_ref_id != a.order_item.sku_id:
            return blocked("sku_mismatch")
        if a.qty_picked and a.status != FbsOrderStockAllocation.STATUS_PICKED:
            return blocked("inconsistent_allocation")
        if a.status in ("reserved", "picking"):
            return blocked("active_reservation")
        picked[a.order_item_id] += a.qty_picked
    quarantined, quarantine_error = _quarantine_evidence(order, allocations, lock=lock)
    if quarantine_error:
        return blocked(quarantine_error)
    excluded_ids = {e["allocation_id"] for e in quarantined}
    for a in allocations:
        if a.pk in excluded_ids:
            picked[a.order_item_id] -= a.qty_picked
    if any(picked[i.pk] != i.quantity for i in items):
        return blocked("debit_not_proven")
    result.update(action="reconcile_status", reason="stock_consumed_at_pick",
                  already_debited_qty=sum(picked.values()),
                  allocation_ids=[a.pk for a in allocations if a.qty_picked and a.pk not in excluded_ids],
                  quarantined_picks=quarantined,
                  quarantined_qty=sum(e["quantity"] for e in quarantined))
    return result


def inspect_delivered_order(order_id):
    order = FbsOrder.objects.select_related("profile").get(pk=order_id)
    return _decision(order)


@transaction.atomic
def reconcile_delivered_order(*, order_id, confirmation, actor=None):
    """Only accept matching normalized marketplace evidence; recheck under locks.

    Callers obtain confirmation from the marketplace reader/ingestion pipeline.
    Replay changes neither quantities nor existing physical handover timestamps.
    """
    order = FbsOrder.objects.select_for_update().select_related("profile").get(pk=order_id)
    if not feature_enabled("module") or not feature_enabled("warehouse_writes"):
        return {"order_id": order.pk, "action": "blocked", "reason": "writes_disabled"}
    status = str(confirmation.get("marketplace_status") or "").strip().lower()
    substatus = str(confirmation.get("marketplace_substatus") or "").strip().lower()
    if (str(confirmation.get("external_order_id") or "") != order.external_order_id
            or confirmation.get("profile_id") != order.profile_id
            or not isinstance(confirmation.get("raw_payload"), dict)
            or not confirmation["raw_payload"]
            or (status, substatus) != (order.marketplace_status.lower(), order.marketplace_substatus.lower())):
        return {"order_id": order.pk, "action": "blocked", "reason": "confirmation_mismatch"}
    result = _decision(order, lock=True)
    if result["action"] != "reconcile_status":
        return result
    previous = order.internal_status
    now = timezone.now()
    # QuerySet.update avoids duplicate model-save audit receivers. The explicit
    # audit below is in the same transaction; an audit failure rolls back status.
    FbsOrder.objects.filter(pk=order.pk).update(internal_status=FbsOrder.STATUS_DELIVERED, updated_at=now)
    order.internal_status = FbsOrder.STATUS_DELIVERED
    entry = log_order_snapshot(
        order, changed_fields=("internal_status",), previous={"internal_status": previous},
        user=actor, source="marketplace_delivery_reconciliation", occurred_at=now,
    )
    entry.payload.update({
        "stock_effect": "already_consumed_at_pick", "debit_qty": 0,
        "already_debited_qty": result["already_debited_qty"],
        "allocation_ids": result["allocation_ids"],
        "quarantined_picks": result["quarantined_picks"],
        "quarantined_qty": result["quarantined_qty"],
        "marketplace_confirmation_sha256": hashlib.sha256(json.dumps(
            confirmation["raw_payload"], sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        ).encode()).hexdigest(),
        "confirmation_source": confirmation.get("source", "marketplace_status_import"),
        "physical_handover_not_reconstructed": True,
    })
    entry.save(update_fields=["payload"])
    return {**result, "action": "reconciled", "audit_id": entry.pk}
