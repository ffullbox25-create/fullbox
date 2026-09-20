from __future__ import annotations

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from sklad.models import WarehouseLocation

from ..models import (
    WmsNewBox,
    WmsNewBoxItem,
    WmsNewEvent,
    WmsNewMovement,
    WmsNewOrder,
    WmsNewProduct,
    WmsNewReturn,
    WmsNewReturnLine,
)


class ReturnOperationError(Exception):
    pass


ACTIVE_STATUSES = (
    WmsNewReturn.STATUS_WAITING,
    WmsNewReturn.STATUS_QUEUED,
    WmsNewReturn.STATUS_IN_PROGRESS,
    WmsNewReturn.STATUS_FAILED,
)


def _event(item: WmsNewReturn, action: str, actor, before=None) -> None:
    WmsNewEvent.objects.create(
        entity_type="return",
        entity_id=item.id,
        action=action,
        actor=actor,
        before=before or {},
        after={
            "status": item.status,
            "returned_qty": item.returned_qty,
            "pilot_revision": item.pilot_revision,
            "order_id": item.order_id,
        },
    )


def _actor_name(actor) -> str:
    if actor is None:
        return ""
    return str(actor.get_full_name() or actor.get_username() or actor).strip()


def _location_name(location: WarehouseLocation | None) -> str:
    if location is None:
        return ""
    return location.display_name or location.location_code or location.zone_code


def _recalculate_box(box: WmsNewBox) -> None:
    totals = box.items.aggregate(
        qty=Sum("qty"),
        available=Sum("available_qty"),
        reserved=Sum("reserved_qty"),
    )
    box.stock_on_hand = int(totals["qty"] or 0)
    box.stock_free = int(totals["available"] or 0)
    box.reserved_qty = int(totals["reserved"] or 0)
    box.sku_count = box.items.filter(qty__gt=0).values("sku_code").distinct().count()
    box.marking_count = box.items.exclude(marking_code="").filter(qty__gt=0).count()
    box.pilot_revision += 1
    box.save()


def _materialize_lines(item: WmsNewReturn) -> None:
    if item.lines.exists():
        return
    order_items = list(item.order.items.select_related("sku").order_by("id"))
    products = {
        row.source_sku_id: row
        for row in WmsNewProduct.objects.filter(
            source_sku_id__in=[row.sku_id for row in order_items if row.sku_id],
            is_archived=False,
        )
    }
    WmsNewReturnLine.objects.bulk_create(
        [
            WmsNewReturnLine(
                return_request=item,
                order_item=order_item,
                product=products.get(order_item.sku_id),
                product_name=order_item.product_name or (order_item.sku.name if order_item.sku else ""),
                article=(order_item.sku.sku_code if order_item.sku else order_item.external_sku),
                barcode=order_item.barcode,
                planned_qty=max(1, int(order_item.quantity or 1)),
            )
            for order_item in order_items
        ]
    )


def create_return(*, scan: str, reason: str = "", actor=None) -> WmsNewReturn:
    scan = str(scan or "").strip()
    if not scan:
        raise ReturnOperationError("Отсканируйте номер или трэк-номер заказа.")
    allowed_order_statuses = (
        WmsNewOrder.STATUS_HANDED_OVER,
        WmsNewOrder.STATUS_DONE,
        WmsNewOrder.STATUS_CANCELLED,
        WmsNewOrder.STATUS_EXCEPTION,
        WmsNewOrder.STATUS_RETURN_PENDING,
        WmsNewOrder.STATUS_RETURNED,
    )
    with transaction.atomic():
        try:
            order = (
                WmsNewOrder.objects.select_for_update()
                .prefetch_related("items")
                .get(
                    Q(external_order_id__iexact=scan) | Q(tracking_number__iexact=scan),
                    status__in=allowed_order_statuses,
                )
            )
        except WmsNewOrder.DoesNotExist as exc:
            raise ReturnOperationError("Заказ для возврата не найден в FBS-NEW.") from exc
        except WmsNewOrder.MultipleObjectsReturned as exc:
            raise ReturnOperationError("По скану найдено несколько заказов. Уточните номер.") from exc
        if WmsNewReturn.objects.filter(order=order, status__in=ACTIVE_STATUSES).exists():
            raise ReturnOperationError("По заказу уже есть активное задание возврата FBS-NEW.")
        planned_qty = order.items.aggregate(total=Sum("quantity"))["total"] or 1
        shipped_at = (
            order.shipment_links.filter(shipment__dispatched_at__isnull=False)
            .order_by("-shipment__dispatched_at")
            .values_list("shipment__dispatched_at", flat=True)
            .first()
        )
        item = WmsNewReturn.objects.create(
            order=order,
            status=WmsNewReturn.STATUS_QUEUED,
            reason=str(reason or "").strip(),
            planned_qty=max(1, int(planned_qty)),
            created_by=actor,
            shipped_at=shipped_at,
            source_created_at=timezone.now(),
            last_synced_at=timezone.now(),
            is_manual=True,
            pilot_revision=1,
        )
        before_order = {"status": order.status, "pilot_revision": order.pilot_revision}
        order.status = WmsNewOrder.STATUS_RETURN_PENDING
        order.pilot_revision += 1
        order.save(update_fields=("status", "pilot_revision", "updated_at"))
        _materialize_lines(item)
        _event(item, "create", actor, before_order)
    return item


@transaction.atomic
def start_receiving(
    *, return_id: int, location_id: int, box_code: str, actor=None
) -> WmsNewReturn:
    item = WmsNewReturn.objects.select_for_update().select_related("order").get(pk=return_id)
    if not item.is_manual:
        raise ReturnOperationError("Перенесенный возврат доступен только для просмотра.")
    if item.status not in {
        WmsNewReturn.STATUS_QUEUED,
        WmsNewReturn.STATUS_FAILED,
        WmsNewReturn.STATUS_IN_PROGRESS,
    }:
        raise ReturnOperationError("Возврат нельзя принять в текущем статусе.")
    location = WarehouseLocation.objects.filter(pk=location_id, is_active=True).first()
    if location is None:
        raise ReturnOperationError("Выбранное место хранения не найдено.")
    clean_code = str(box_code or "").strip()
    if not clean_code:
        clean_code = f"FBN-RETURN-{item.id}"
    box, created = WmsNewBox.objects.select_for_update().get_or_create(
        agency=item.order.agency,
        code=clean_code,
        defaults={
            "location": location,
            "location_code": _location_name(location),
            "zone_code": location.zone_code,
            "is_manual": True,
            "pilot_revision": 1,
            "created_by": actor,
        },
    )
    if not created and box.status != WmsNewBox.STATUS_ACTIVE:
        raise ReturnOperationError("Выбранный короб неактивен.")
    if not created and box.location_id not in {None, location.id}:
        raise ReturnOperationError("Выбранный короб находится в другом месте хранения.")
    if box.location_id is None:
        box.location = location
        box.location_code = _location_name(location)
        box.zone_code = location.zone_code
        box.pilot_revision += 1
        box.save()
    _materialize_lines(item)
    before = {"status": item.status, "pilot_revision": item.pilot_revision}
    item.status = WmsNewReturn.STATUS_IN_PROGRESS
    item.assigned_to = actor
    item.received_by = actor
    item.destination_location = location
    item.return_box = box
    item.receiving_started_at = timezone.now()
    item.pilot_revision += 1
    item.save()
    _event(item, "start_receiving", actor, before)
    return item


@transaction.atomic
def record_return_line(
    *, return_id: int, line_id: int, received_qty, accepted_qty,
    condition: str, marking_codes: str | list[str] = "", actor=None
) -> WmsNewReturnLine:
    item = WmsNewReturn.objects.select_for_update().get(pk=return_id)
    if item.status != WmsNewReturn.STATUS_IN_PROGRESS or not item.is_manual:
        raise ReturnOperationError("Возврат сейчас не принимает результаты осмотра.")
    line = WmsNewReturnLine.objects.select_for_update(of=("self",)).select_related("product").get(
        pk=line_id,
        return_request=item,
    )
    try:
        received = int(received_qty)
        accepted = int(accepted_qty)
    except (TypeError, ValueError) as exc:
        raise ReturnOperationError("Количество должно быть целым числом.") from exc
    if received < 0 or received > line.planned_qty:
        raise ReturnOperationError("Принятое количество выходит за пределы задания.")
    if accepted < 0 or accepted > received:
        raise ReturnOperationError("Годное количество не может превышать принятое.")
    if condition not in dict(WmsNewReturnLine.CONDITION_CHOICES):
        raise ReturnOperationError("Укажите результат осмотра товара.")
    if isinstance(marking_codes, str):
        codes = [value.strip() for value in marking_codes.replace(";", "\n").splitlines() if value.strip()]
    else:
        codes = [str(value).strip() for value in marking_codes if str(value).strip()]
    if len(codes) != len(set(code.casefold() for code in codes)):
        raise ReturnOperationError("В списке есть повторяющиеся КИЗ.")
    if line.product and line.product.marking_required and accepted != len(codes):
        raise ReturnOperationError("Для каждой принятой маркированной единицы отсканируйте КИЗ.")
    if (not line.product or not line.product.marking_required) and codes:
        raise ReturnOperationError("КИЗ можно указывать только для маркированного товара.")
    line.received_qty = received
    line.accepted_qty = accepted
    line.rejected_qty = received - accepted
    line.condition = condition
    line.marking_codes = codes
    line.updated_by = actor
    line.save()
    item.returned_qty = item.lines.aggregate(total=Sum("received_qty"))["total"] or 0
    item.pilot_revision += 1
    item.save(update_fields=("returned_qty", "pilot_revision", "updated_at"))
    _event(item, "inspect_line", actor)
    return line


def _put_return_line_to_stock(item: WmsNewReturn, line: WmsNewReturnLine, actor=None) -> None:
    if not line.accepted_qty:
        return
    if line.product_id is None:
        raise ReturnOperationError(
            f"Товар «{line.product_name}» не сопоставлен с карточкой FBS-NEW."
        )
    product = WmsNewProduct.objects.select_for_update().get(pk=line.product_id)
    box = WmsNewBox.objects.select_for_update().get(pk=item.return_box_id)
    product.stock_on_hand += line.accepted_qty
    product.stock_free += line.accepted_qty
    product.pilot_revision += 1
    product.save(update_fields=("stock_on_hand", "stock_free", "pilot_revision", "updated_at"))
    codes = line.marking_codes if product.marking_required else [""]
    for code in codes:
        quantity = 1 if code else line.accepted_qty
        box_item = WmsNewBoxItem.objects.select_for_update().filter(
            box=box,
            product=product,
            barcode=line.barcode,
            marking_code=code,
        ).first()
        if box_item is None:
            WmsNewBoxItem.objects.create(
                box=box,
                product=product,
                agency_id=item.order.agency_id,
                sku_code=line.article,
                product_name=line.product_name,
                size=product.size,
                barcode=line.barcode,
                marking_code=code,
                qty=quantity,
                available_qty=quantity,
                pilot_revision=1,
                source_snapshot={"origin": "wms_new_return", "return_id": item.id},
            )
        else:
            box_item.qty += quantity
            box_item.available_qty += quantity
            box_item.pilot_revision += 1
            box_item.save()
    _recalculate_box(box)
    WmsNewMovement.objects.create(
        agency_id=item.order.agency_id,
        product=product,
        product_name=line.product_name,
        article=line.article,
        action=WmsNewMovement.ACTION_RECEIPT,
        target_location=item.destination_location,
        target_location_name=_location_name(item.destination_location),
        quantity=line.accepted_qty,
        balance_after=product.stock_on_hand,
        boxes_count=1,
        information=f"Возврат FBS-NEW №{item.id}",
        source_event_type="wms_new_return",
        actor=actor,
        actor_name=_actor_name(actor),
        occurred_at=timezone.now(),
        payload={"return_id": item.id, "line_id": line.id, "box_id": box.id},
        is_manual=True,
    )


@transaction.atomic
def complete_return(*, return_id: int, actor=None) -> WmsNewReturn:
    item = (
        WmsNewReturn.objects.select_for_update(of=("self",))
        .select_related("order", "destination_location", "return_box")
        .get(pk=return_id)
    )
    if item.status != WmsNewReturn.STATUS_IN_PROGRESS or not item.is_manual:
        raise ReturnOperationError("Возврат нельзя завершить в текущем статусе.")
    if item.destination_location_id is None or item.return_box_id is None:
        raise ReturnOperationError("Сначала выберите место и короб приемки.")
    lines = list(
        item.lines.select_for_update(of=("self",)).select_related("product").order_by("id")
    )
    if not lines:
        raise ReturnOperationError("В возврате нет товарных строк.")
    if any(line.received_qty < line.planned_qty for line in lines):
        raise ReturnOperationError("Осмотрите все запланированные единицы возврата.")
    for line in lines:
        _put_return_line_to_stock(item, line, actor=actor)
    before = {"status": item.status, "pilot_revision": item.pilot_revision}
    item.returned_qty = sum(line.received_qty for line in lines)
    item.status = WmsNewReturn.STATUS_COMPLETED
    item.returned_at = timezone.now()
    item.pilot_revision += 1
    item.save()
    item.order.status = WmsNewOrder.STATUS_RETURNED
    item.order.pilot_revision += 1
    item.order.save(update_fields=("status", "pilot_revision", "updated_at"))
    _event(item, "complete_receiving", actor, before)
    return item


def update_returns(*, return_ids: list[int], action: str, actor=None) -> int:
    transitions = {
        "start": ({WmsNewReturn.STATUS_QUEUED, WmsNewReturn.STATUS_FAILED}, WmsNewReturn.STATUS_IN_PROGRESS),
        "fail": ({WmsNewReturn.STATUS_QUEUED, WmsNewReturn.STATUS_IN_PROGRESS}, WmsNewReturn.STATUS_FAILED),
        "cancel": ({WmsNewReturn.STATUS_WAITING, WmsNewReturn.STATUS_QUEUED, WmsNewReturn.STATUS_FAILED}, WmsNewReturn.STATUS_CANCELLED),
    }
    rule = transitions.get(action)
    if rule is None:
        raise ReturnOperationError("Неизвестное действие с возвратами.")
    unique_ids = list(dict.fromkeys(int(value) for value in return_ids))
    if not unique_ids:
        raise ReturnOperationError("Выберите хотя бы один возврат.")
    with transaction.atomic():
        items = list(
            WmsNewReturn.objects.select_for_update().select_related("order").filter(id__in=unique_ids)
        )
        if len(items) != len(unique_ids):
            raise ReturnOperationError("Часть возвратов FBS-NEW не найдена.")
        allowed, target_status = rule
        for item in items:
            if item.status not in allowed:
                raise ReturnOperationError(
                    f"Действие недоступно для возврата #{item.source_request_id or item.id}."
                )
        for item in items:
            before = {
                "status": item.status,
                "returned_qty": item.returned_qty,
                "pilot_revision": item.pilot_revision,
            }
            item.status = target_status
            item.pilot_revision += 1
            if target_status == WmsNewReturn.STATUS_IN_PROGRESS:
                item.assigned_to = actor
            if target_status == WmsNewReturn.STATUS_CANCELLED:
                item.order.status = WmsNewOrder.STATUS_HANDED_OVER
                item.order.pilot_revision += 1
                item.order.save(update_fields=("status", "pilot_revision", "updated_at"))
            item.save()
            _event(item, action, actor, before)
    return len(items)
