"""Durable quantity requests; only dispatchable stock becomes a physical task."""
from collections import defaultdict
from contextvars import ContextVar

from django.db import transaction
from django.db.models import F
from django.http import JsonResponse
from audit.models import OrderAuditEntry
from sku.models import Agency
from reachtruck.models import MoveRequest, MoveRequestItem, MoveTask, BoxClaim
from reachtruck.services.claims import claim_boxes_for_task, release_claims_for_task
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sklad.services.operational_locations import select_operational_location

draining = ContextVar("obr_queue_draining", default=False)


def quantity_order_payload(agency, order_key):
    for payload in OrderAuditEntry.objects.filter(agency=agency, order_type="processing", order_id=order_key).order_by("-created_at", "-id").values_list("payload", flat=True):
        if isinstance(payload, dict) and ("stock_rows" in payload or "cards" in payload):
            return payload if payload.get("client_unit_picker_v1") is True else None
    return None


def queue_record(move_request):
    return OrderAuditEntry.objects.filter(agency_id=move_request.agency_id, order_type="processing",
        order_id=move_request.context_id, payload__obr_quantity_queue_id=move_request.pk).first()


def order_cancelled(agency_id, order_key):
    from processing_app.stages import processing_is_cancelled
    for payload in OrderAuditEntry.objects.filter(agency_id=agency_id, order_type="processing",
            order_id=order_key).order_by("-created_at", "-id").values_list("payload", flat=True):
        if isinstance(payload, dict) and any(key in payload for key in ("status", "processing_stage", "submit_action")):
            return processing_is_cancelled(payload)
    return False


def committed_quantities(move_request):
    quantities = defaultdict(int)
    for task in move_request.tasks.all():
        item_id = (task.payload or {}).get("quantity_queue_item_id")
        if not item_id:
            continue
        amount = task.qty_done if task.status in {MoveTask.STATUS_DONE, MoveTask.STATUS_CANCELED} else task.qty_planned
        quantities[int(item_id)] += int(amount or 0)
    return quantities


def waiting_quantity(move_request):
    if not queue_record(move_request):
        return 0
    committed = committed_quantities(move_request)
    return sum(max(row.qty_requested - committed[row.pk], 0) for row in move_request.items.all())


def _dispatch_rows(move_request, item, rows):
    from .services import (
        _processing_reserve_items,
        _task_payload,
        processing_order_requires_concrete_location,
    )
    concrete_location_required = processing_order_requires_concrete_location(
        agency=move_request.agency,
        processing_order_id=move_request.context_id,
    )
    task_concrete_location_required = concrete_location_required
    destination_slots = len({
        (str(row.get("pallet_code") or "").strip(), bool(row.get("is_partial_pick")))
        for row in rows
        if str(row.get("pallet_code") or "").strip()
    })
    if concrete_location_required:
        destination_location = select_operational_location(
            zone_code="OBR",
            required_slots=max(destination_slots, 1),
            lock=True,
        )
        if destination_location is None:
            destination_location = WarehouseWritePathService.ensure_location(
                warehouse_code="MSK",
                zone_code="OBR",
            )
            task_concrete_location_required = False
    else:
        destination_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )
    reserve_items = _processing_reserve_items(rows)
    WarehouseWritePathService.reserve_for_processing(agency=move_request.agency,
        order_id=move_request.context_id, items=reserve_items, created_by=move_request.requested_by,
        source_document_type="processing_obr_request", source_document_id=move_request.context_id)
    groups = defaultdict(list)
    for row in rows:
        groups[(row["pallet_code"], bool(row.get("is_partial_pick")))].append(row)
    for (pallet, partial), picked in groups.items():
        payload = _task_payload(move_request=move_request, processing_order_id=move_request.context_id,
            rows=picked, pallet_code=pallet, full_pallet=False,
            employee_name=move_request.requested_by_name, employee_role=move_request.requested_by_role,
            destination_location=destination_location,
            concrete_location_required=task_concrete_location_required)
        payload.update(obr_requires_box_scans=True, quantity_queue_item_id=item.pk,
            processing_pick_fact_mode="scanned_units_v1" if partial else "scanned_boxes_v1")
        location = picked[0].get("from_location") or {}
        task = MoveTask.objects.create(request=move_request, pallet_code=pallet,
            from_zone=location.get("zone") or "OS", to_zone="OBR", move_mode=payload["move_mode"],
            from_row=int(location.get("row") or 0) or None,
            from_section=int(location.get("section") or 0) or None,
            from_tier=int(location.get("tier") or 0) or None,
            from_cell=int(location.get("cell") or 0) or None,
            qty_planned=sum(int(r["qty"]) for r in picked), payload=payload)
        task.legacy_order_id = f"PROC-OBR-{move_request.pk}-Q{task.pk}"
        payload.update(move_task_id=task.pk, legacy_move_id=task.legacy_order_id)
        task.payload = payload
        task.save(update_fields=["legacy_order_id", "payload", "updated_at"])
        claim_boxes_for_task(task, payload.get("requested_boxes") or [], claimed_by=move_request.requested_by,
            claim_kind=BoxClaim.KIND_PARTIAL if partial else BoxClaim.KIND_BOX,
            payload={"processing_order_id": move_request.context_id, "quantity_queue": True}, lock_pallet=False)


def _cancel_locked(move_request):
    """Cancel a closed queue completely without erasing physical stock facts."""
    tasks = list(move_request.tasks.select_for_update().order_by("pk"))
    unsafe_tasks = []
    for task in tasks:
        if task.status in {MoveTask.STATUS_DONE, MoveTask.STATUS_CANCELED}:
            continue
        execution = (task.payload or {}).get("mobile_execution") or {}
        if int(task.qty_done or 0) > 0 or (
            execution.get("source_confirmed") and not execution.get("destination_confirmed")
        ):
            unsafe_tasks.append(str(task.legacy_order_id or task.pk))
    if unsafe_tasks:
        move_request.status = MoveRequest.STATUS_BLOCKED
        move_request.planning_error = (
            "Отмена заблокирована: есть начатые перемещения без подтверждения назначения: "
            + ", ".join(unsafe_tasks[:5])
        )
        move_request.save(update_fields=["status", "planning_error", "updated_at"])
        return False

    for task in tasks:
        if task.status == MoveTask.STATUS_DONE:
            # Repair a stale claim/lock left behind by an already delivered task.
            release_claims_for_task(task, delivered=True)
            continue
        if task.status != MoveTask.STATUS_CANCELED:
            task.status = MoveTask.STATUS_CANCELED
            payload = dict(task.payload or {})
            payload["status"] = MoveTask.STATUS_CANCELED
            payload["status_label"] = "Отменено вместе с заявкой на перемещение"
            task.payload = payload
            task.save(update_fields=["status", "payload", "updated_at"])
        release_claims_for_task(task, delivered=False)

    WarehouseWritePathService.replace_processing_reserves(
        agency=move_request.agency,
        order_id=move_request.context_id,
        items=[],
        created_by=move_request.requested_by,
    )
    move_request.status = MoveRequest.STATUS_CANCELED
    move_request.planning_error = ""
    move_request.save(update_fields=["status", "planning_error", "updated_at"])
    return True


def _resume_locked(move_request):
    """Caller holds agency lock then request lock; no old reserves are replaced."""
    from .services import build_obr_requested_rows
    if (
        move_request.status == MoveRequest.STATUS_CANCELED
        or order_cancelled(move_request.agency_id, move_request.context_id)
        or move_request.tasks.filter(status=MoveTask.STATUS_CANCELED).exists()
    ):
        _cancel_locked(move_request)
        return
    record = queue_record(move_request)
    if not record:
        return
    committed = committed_quantities(move_request)
    saved = record.payload["queue_items"]
    for item in move_request.items.order_by("pk"):
        demand = dict(saved[str(item.pk)])
        need = max(item.qty_requested - committed[item.pk], 0)
        if need:
            demand["requested_qty"] = need
            rows = build_obr_requested_rows(agency=move_request.agency,
                processing_order_id=move_request.context_id, request_items=[demand], allow_partial=True)
            if rows:
                # A competing claim must roll back both task and reserve.
                try:
                    with transaction.atomic():
                        _dispatch_rows(move_request, item, rows)
                    committed[item.pk] += sum(int(r["qty"]) for r in rows)
                except ValueError:
                    # Availability changed after planning: leave demand queued.
                    pass
        item.qty_planned = committed[item.pk]
        item.save(update_fields=["qty_planned", "updated_at"])
    pending = sum(max(i.qty_requested - committed[i.pk], 0) for i in move_request.items.all())
    tasks = list(move_request.tasks.values_list("status", flat=True))
    if pending:
        move_request.status = MoveRequest.STATUS_PARTIAL if tasks else MoveRequest.STATUS_BLOCKED
        move_request.planning_error = (
            f"В очереди: {pending} шт. Нет свободного подходящего остатка; "
            "система повторит планирование автоматически."
        )
    else:
        move_request.planning_error = ""
        move_request.status = (MoveRequest.STATUS_DONE if tasks and all(s == MoveTask.STATUS_DONE for s in tasks)
            else MoveRequest.STATUS_IN_PROGRESS if MoveTask.STATUS_IN_PROGRESS in tasks else MoveRequest.STATUS_PLANNED)
    move_request.save(update_fields=["status", "planning_error", "updated_at"])


def resume_agency(agency_id):
    if draining.get():
        return
    token = draining.set(True)
    try:
        ids = list(OrderAuditEntry.objects.filter(agency_id=agency_id, order_type="processing",
            payload__has_key="obr_quantity_queue_id").values_list("payload__obr_quantity_queue_id", flat=True))
        if not ids:
            return
        with transaction.atomic():
            agency = Agency.objects.select_for_update().get(pk=agency_id)
            for move_request in MoveRequest.objects.select_for_update().filter(pk__in=ids, agency=agency).exclude(
                status=MoveRequest.STATUS_DONE).order_by("created_at", "pk"):
                _resume_locked(move_request)
    finally:
        draining.reset(token)


@transaction.atomic
def create_quantity_queue(*, request, agency, order_key, request_items):
    from .services import _remaining_processing_request_items, _employee_name
    from employees.access import get_request_employee, get_request_role
    agency = Agency.objects.select_for_update().get(pk=agency.pk)
    if order_cancelled(agency.pk, order_key):
        return JsonResponse({"ok": False, "error": "Заявка обработки отменена."}, status=400)
    existing = MoveRequest.objects.select_for_update().filter(agency=agency,
        context_type="processing", context_id=order_key, destination_zone="OBR").exclude(
        status__in=[MoveRequest.STATUS_CANCELED, MoveRequest.STATUS_DONE]).order_by("pk").first()
    if existing:
        if not queue_record(existing):
            return JsonResponse({"ok": False, "error": "Для этой обработки уже есть активное задание в OBR."}, status=400)
        _resume_locked(existing)
        return _response(existing)
    items = _remaining_processing_request_items(agency=agency, processing_order_id=order_key, request_items=request_items)
    if not items:
        return JsonResponse({"ok": False, "error": "Товар уже доставлен в OBR."}, status=400)
    user = getattr(request, "user", None)
    user = user if user and user.is_authenticated else None
    # Release only the same order's provisional reservation, never a neighbour's.
    WarehouseWritePathService.replace_processing_reserves(agency=agency, order_id=order_key, items=[], created_by=user)
    move_request = MoveRequest.objects.create(agency=agency, context_type="processing", context_id=order_key,
        destination_zone="OBR", requested_by=user, requested_by_role=get_request_role(request) or "processing_head",
        requested_by_name=_employee_name(get_request_employee(request), user), status=MoveRequest.STATUS_BLOCKED)
    saved = {}
    for demand in items:
        item = MoveRequestItem.objects.create(request=move_request,
            sku_code=str(demand.get("requested_article") or "")[:64],
            barcode=str((demand.get("requested_barcodes") or [""])[0])[:64],
            goods_type=str(demand.get("requested_goods_type") or "")[:32],
            qty_requested=int(demand.get("requested_qty") or demand.get("qty") or 0))
        saved[str(item.pk)] = demand
    OrderAuditEntry.objects.create(agency=agency, order_type="processing", order_id=order_key,
        action="status", payload={"obr_quantity_queue_id": move_request.pk, "queue_items": saved})
    _resume_locked(move_request)
    return _response(move_request)


def _response(move_request):
    pending = waiting_quantity(move_request)
    planned = sum(i.qty_planned for i in move_request.items.all())
    return JsonResponse({"ok": True, "request_id": move_request.pk, "tasks_created": move_request.tasks.count(),
        "status": move_request.status, "queued_qty": pending, "shortage_qty": pending,
        "planned_qty": planned, "message": f"Принято. Спланировано: {planned} шт. В очереди: {pending} шт."})
