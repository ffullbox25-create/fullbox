"""Explicit replacement destinations for missing original return boxes."""
from django.db import transaction
from django.db.models import Q, F
from django.utils import timezone

from fbs.exceptions import FbsError, FbsPickingError
from fbs.models import FbsBox, FbsPickRestockRequest, FbsStockBalance
from sklad.models import WarehouseContainer, WarehouseEvent
from .physical_locations import (
    fbs_box_physical_location, fbs_box_physical_location_code,
    fbs_box_physical_location_label, is_virtual_fbs_plan_location,
)
from .inventory import assert_balance_unlocked, assert_box_unlocked

EVENT_TYPE = "fbs_restock_destination_selected"


def destination_event(request_id, source_box_id):
    return WarehouseEvent.objects.filter(
        event_type=EVENT_TYPE, stock_context_type="fbs_pick_restock",
        stock_context_id=str(request_id), payload__source_box_id=source_box_id,
    ).order_by("-id").first()


def replacement_box(event):
    if event is None:
        return None
    box = FbsBox.objects.select_related(
        "pallet__cell__location", "source_container__current_location",
    ).filter(pk=event.payload["destination_box_id"]).first()
    if box is None:
        raise FbsPickingError("Выбранный короб возврата недоступен. Выберите другой короб.")
    return box


def assert_destination(box, agency_id, *, selected_location_id=None):
    """Rechecked under the return transaction on every placement scan."""
    if box.agency_id != agency_id or box.pallet.agency_id != agency_id:
        raise FbsPickingError("Для возврата нужен короб того же клиента.")
    if box.status != FbsBox.STATUS_ACTIVE or box.pallet.status != "active" or not box.pallet.cell.is_active:
        raise FbsPickingError("Короб, паллета или ячейка недоступны для возврата.")
    container = box.source_container
    location = fbs_box_physical_location(box)
    if container is not None and (container.agency_id != agency_id or container.status != "active"):
        raise FbsPickingError("Складской короб недоступен или принадлежит другому клиенту.")
    if location is None or is_virtual_fbs_plan_location(location) or not location.is_active:
        raise FbsPickingError("Физическое место короба не определено или неактивно.")
    receiving = (
        location.zone_code == "PR" and location.zone_kind == "receiving"
        and container is not None and container.current_location_id == location.pk
    )
    if not (location.is_storage or receiving):
        raise FbsPickingError("Это место не подходит для возврата товара.")
    if selected_location_id is not None and location.pk != selected_location_id:
        raise FbsPickingError("Выбранный короб перемещён. Выберите место возврата повторно.")
    if box.stock_balances.exclude(agency_id=agency_id).filter(Q(qty__gt=0) | Q(reserved_qty__gt=0)).exists():
        raise FbsPickingError("В коробе обнаружен товар другого клиента.")
    if box.quarantine_pick_restock_requests.exists() or box.stock_balances.filter(qty__gt=F("available_qty")+F("reserved_qty")).exists():
        raise FbsPickingError("Короб содержит карантинный или недоступный товар.")
    assert_box_unlocked(box.pk, for_execution=True)
    return location


def destination_choices(state, limit=5):
    if not state.get("line") or state["is_quarantine"] or not state["pickup_confirmed"]:
        return []
    line = state["line"]
    origin = fbs_box_physical_location(state["destination_box"])
    agency_id = line.source_box.agency_id
    boxes = FbsBox.objects.filter(
        agency_id=agency_id, status=FbsBox.STATUS_ACTIVE,
        pallet__status="active", pallet__cell__is_active=True,
    ).exclude(pk__in=[state["destination_box"].pk,line.source_box_id]).select_related(
        "pallet__cell__location", "source_container__current_location",
    )
    # Ranking is topological proximity, not a claim about walking distance.
    ranked = []
    for box in boxes:
        location = fbs_box_physical_location(box)
        if location is None or is_virtual_fbs_plan_location(location) or not location.is_active:
            continue
        same_place = origin is not None and origin.pk == location.pk
        same_zone = origin is not None and origin.warehouse_code == location.warehouse_code and origin.zone_code == location.zone_code
        coords = ("row_no", "section_no", "tier_no", "cell_no")
        known = origin is not None and all(getattr(origin, f, 0) and getattr(location, f, 0) for f in coords)
        distance = tuple(abs(getattr(origin, f)-getattr(location, f)) for f in coords) if known else (9999,)*4
        ranked.append(((0 if same_place else 1 if same_zone else 2, distance, box.box_code), box))
    choices = []
    for _, box in sorted(ranked, key=lambda row: row[0]):
        try:
            assert_destination(box, agency_id)
        except FbsError:
            continue
        choices.append({"box_code":box.box_code, "location":fbs_box_physical_location_label(box)})
        if len(choices) >= limit:
            break
    return choices


@transaction.atomic
def select_destination(*, request_id, line_id, box_scan, performed_by, space_confirmed):
    from .pick_restock import _actor, _require_writes, pick_restock_state
    _require_writes()
    actor = _actor(performed_by)
    request = FbsPickRestockRequest.objects.select_for_update().get(pk=request_id)
    if request.status != FbsPickRestockRequest.STATUS_IN_PROGRESS or request.assigned_to_id != actor.pk:
        raise FbsPickingError("Возврат должен находиться в работе у вас.")
    if request.quarantine_box_id:
        raise FbsPickingError("Карантинный короб назначает контролер.")
    state = pick_restock_state(request.pk)
    line = state["line"]
    if line is None or str(line.pk) != str(line_id) or not state["pickup_confirmed"]:
        raise FbsPickingError("Экран возврата изменился. Сначала заберите товар из тары.")
    if not space_confirmed:
        raise FbsPickingError("Подтвердите, что в выбранном коробе достаточно свободного места.")
    boxes = list(FbsBox.objects.select_for_update(of=("self",)).select_related(
        "pallet__cell__location", "source_container__current_location",
    ).filter(box_code__iexact=str(box_scan or "").strip())[:2])
    if len(boxes) != 1:
        raise FbsPickingError("Короб не найден. Отсканируйте QR существующего короба клиента.")
    box = boxes[0]
    if box.source_container_id:
        box.source_container = WarehouseContainer.objects.select_for_update(of=("self",)).select_related("current_location").get(pk=box.source_container_id)
    location = assert_destination(box, line.source_box.agency_id)
    previous = destination_event(request.pk, line.source_box_id)
    if previous is not None and previous.payload["destination_box_id"] == box.pk and previous.payload["destination_location_id"] == location.pk:
        return previous
    return WarehouseEvent.objects.create(
        agency_id=line.source_box.agency_id, event_type=EVENT_TYPE,
        stock_context_type="fbs_pick_restock", stock_context_id=str(request.pk),
        source_document_type="fbs_pick_restock", source_document_id=str(request.pk),
        container=box.source_container, to_location=location, to_zone_code=location.zone_code,
        qty=0, performed_by=actor, performed_by_role="picker", occurred_at=timezone.now(),
        payload={"source_box_id":line.source_box_id, "destination_box_id":box.pk,
                 "destination_location_id":location.pk, "line_id":line.pk,
                 "reason":"original_box_missing", "space_confirmed":True},
    )


def restore_to_destination(*, allocation, balance, box):
    """Credit one scanned unit; preserve the original allocation and its identity."""
    if not transaction.get_connection().in_atomic_block:
        raise FbsPickingError("Возврат должен выполняться внутри складской операции.")
    if allocation.balance_id != balance.pk or int(allocation.qty_picked or 0) <= 0:
        raise FbsPickingError("Эта единица уже возвращена или не относится к отбору.")
    assert_destination(box, balance.agency_id)
    if box.pk == balance.box_id:
        return None
    if balance.marking_code:
        if balance.qty or balance.reserved_qty:
            raise FbsPickingError("КИЗ уже числится в остатке. Требуется проверка.")
        balance.box = box
        balance.qty = 1
        balance.available_qty = 1
        balance.full_clean()
        balance.save(update_fields=["box", "qty", "available_qty", "updated_at"])
        return balance
    target, _ = FbsStockBalance.objects.select_for_update().get_or_create(
        box=box, identity_key=balance.identity_key,
        defaults={**{f:getattr(balance,f) for f in (
            "agency_id","sku_ref_id","sku_code","name","size","barcode",
            "goods_type","marking_code","lot_code","expiry_date",
        )}, "qty":0,"available_qty":0,"reserved_qty":0},
    )
    assert_balance_unlocked(target.pk, for_execution=True)
    if any(getattr(target,f) != getattr(balance,f) for f in ("agency_id","sku_code","size","barcode","marking_code","lot_code","expiry_date")):
        raise FbsPickingError("Состав целевого остатка не совпадает с возвращаемым товаром.")
    target.qty += 1
    target.available_qty += 1
    target.full_clean()
    target.save(update_fields=["qty","available_qty","updated_at"])
    return target
