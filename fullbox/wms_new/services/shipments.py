from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from sku.models import Agency

from ..models import (
    WmsNewEvent,
    WmsNewOrder,
    WmsNewShipment,
    WmsNewShipmentBox,
    WmsNewShipmentOrder,
    WmsNewShipmentService,
)


class ShipmentOperationError(Exception):
    pass


ACTIVE_STATUSES = (
    WmsNewShipment.STATUS_NEW,
    WmsNewShipment.STATUS_CHECKING,
    WmsNewShipment.STATUS_CHECKED,
    WmsNewShipment.STATUS_IN_TRANSIT,
)


def _decimal(value, label: str) -> Decimal:
    try:
        result = Decimal(str(value or "0").replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ShipmentOperationError(f"Поле «{label}» заполнено неверно.") from exc
    if result < 0:
        raise ShipmentOperationError(f"Поле «{label}» не может быть отрицательным.")
    return result


def _event(shipment: WmsNewShipment, action: str, actor, before=None, metadata=None) -> None:
    WmsNewEvent.objects.create(
        entity_type="shipment",
        entity_id=shipment.id,
        action=action,
        actor=actor,
        before=before or {},
        after={
            "status": shipment.status,
            "order_count": shipment.order_count,
            "item_count": shipment.item_count,
            "pilot_revision": shipment.pilot_revision,
            "tariff_finalized": shipment.tariff_finalized,
        },
        metadata=metadata or {},
    )


def _refresh_totals(shipment: WmsNewShipment) -> None:
    links = shipment.shipment_orders.select_related("order").prefetch_related("order__items")
    order_count = 0
    item_count = 0
    total_weight = Decimal("0")
    total_volume = Decimal("0")
    for link in links:
        order_count += 1
        item_count += sum(max(1, int(item.quantity or 1)) for item in link.order.items.all())
        total_weight += link.weight_kg or Decimal("0")
        total_volume += link.volume_l or Decimal("0")
    shipment.order_count = order_count
    shipment.item_count = item_count
    shipment.total_weight_kg = total_weight
    shipment.total_volume_l = total_volume


def _order_metrics(order: WmsNewOrder) -> tuple[Decimal, Decimal, int]:
    weight = Decimal("0")
    volume = Decimal("0")
    units = 0
    for item in order.items.all():
        quantity = max(1, int(item.quantity or 1))
        units += quantity
        sku = item.sku
        if sku is None:
            continue
        unit_weight = Decimal(
            str(sku.weight_gross_kg or sku.weight_net_kg or sku.weight_kg or 0)
        )
        unit_volume = Decimal(str(sku.volume or 0))
        if unit_volume <= 0 and all((sku.length_mm, sku.width_mm, sku.height_mm)):
            unit_volume = (
                Decimal(str(sku.length_mm))
                * Decimal(str(sku.width_mm))
                * Decimal(str(sku.height_mm))
                / Decimal("1000000")
            )
        weight += unit_weight * quantity
        volume += unit_volume * quantity
    return weight, volume, units


def create_shipment(
    *, agency: Agency, delivery_type: str, integration_name: str, actor=None
) -> WmsNewShipment:
    delivery_type = str(delivery_type or "").strip()
    integration_name = str(integration_name or "").strip()
    if not delivery_type:
        raise ShipmentOperationError("Укажите тип доставки.")
    if not integration_name:
        raise ShipmentOperationError("Укажите интеграцию.")
    with transaction.atomic():
        shipment = WmsNewShipment.objects.create(
            agency=agency,
            delivery_type=delivery_type[:128],
            integration_name=integration_name[:128],
            external_name=f"FBS-NEW-{timezone.localtime():%Y%m%d}-{uuid4().hex[:6].upper()}",
            status=WmsNewShipment.STATUS_NEW,
            created_by=actor,
            source_created_at=timezone.now(),
            last_synced_at=timezone.now(),
            is_manual=True,
            pilot_revision=1,
        )
        _event(shipment, "create", actor)
    return shipment


def add_order(*, shipment_id: int, order_id: int, actor=None) -> WmsNewShipmentOrder:
    with transaction.atomic():
        try:
            shipment = WmsNewShipment.objects.select_for_update().get(pk=shipment_id)
        except WmsNewShipment.DoesNotExist as exc:
            raise ShipmentOperationError("Отгрузка FBS-NEW не найдена.") from exc
        if shipment.status not in (WmsNewShipment.STATUS_NEW, WmsNewShipment.STATUS_CHECKING):
            raise ShipmentOperationError("В отгрузку в этом статусе нельзя добавлять заказы.")
        try:
            order = WmsNewOrder.objects.select_for_update().prefetch_related("items__sku").get(
                pk=order_id
            )
        except WmsNewOrder.DoesNotExist as exc:
            raise ShipmentOperationError("Заказ FBS-NEW не найден.") from exc
        if order.agency_id != shipment.agency_id:
            raise ShipmentOperationError("Партнер заказа не совпадает с партнером отгрузки.")
        if order.status != WmsNewOrder.STATUS_READY:
            raise ShipmentOperationError("Добавлять можно только готовые к передаче заказы.")
        conflict = WmsNewShipmentOrder.objects.filter(
            order=order, shipment__status__in=ACTIVE_STATUSES
        ).exclude(shipment=shipment)
        if conflict.exists():
            raise ShipmentOperationError("Заказ уже находится в другой активной отгрузке.")
        if WmsNewShipmentOrder.objects.filter(shipment=shipment, order=order).exists():
            raise ShipmentOperationError("Заказ уже добавлен в эту отгрузку.")
        weight, volume, _ = _order_metrics(order)
        link = WmsNewShipmentOrder.objects.create(
            shipment=shipment,
            order=order,
            sticker_number=order.tracking_number,
            weight_kg=weight,
            volume_l=volume,
            marketplace_state=str((order.source_snapshot or {}).get("supply_status") or "").strip(),
            in_supply=bool((order.source_snapshot or {}).get("in_supply", True)),
            added_by=actor,
        )
        before = {"order_count": shipment.order_count, "pilot_revision": shipment.pilot_revision}
        _refresh_totals(shipment)
        shipment.pilot_revision += 1
        shipment.save()
        _event(shipment, "add_order", actor, before, {"order_id": order.id})
    return link


def remove_order(*, shipment_id: int, shipment_order_id: int, actor=None) -> None:
    with transaction.atomic():
        shipment = WmsNewShipment.objects.select_for_update().get(pk=shipment_id)
        if shipment.status not in (WmsNewShipment.STATUS_NEW, WmsNewShipment.STATUS_CHECKING):
            raise ShipmentOperationError("Заказ можно удалить только до завершения проверки.")
        link = WmsNewShipmentOrder.objects.select_for_update().get(
            pk=shipment_order_id,
            shipment=shipment,
        )
        order_id = link.order_id
        link.delete()
        before = {"order_count": shipment.order_count, "pilot_revision": shipment.pilot_revision}
        _refresh_totals(shipment)
        shipment.pilot_revision += 1
        shipment.save()
        _event(shipment, "remove_order", actor, before, {"order_id": order_id})


def add_box(*, shipment_id: int, qr_code: str, actor=None) -> WmsNewShipmentBox:
    qr_code = str(qr_code or "").strip()
    if not qr_code:
        raise ShipmentOperationError("Укажите QR-код короба.")
    with transaction.atomic():
        shipment = WmsNewShipment.objects.select_for_update().get(pk=shipment_id)
        if shipment.status not in (WmsNewShipment.STATUS_NEW, WmsNewShipment.STATUS_CHECKING):
            raise ShipmentOperationError("В отгрузку в этом статусе нельзя добавлять короба.")
        if shipment.boxes.filter(qr_code=qr_code).exists():
            raise ShipmentOperationError("Короб с таким QR-кодом уже добавлен.")
        box = WmsNewShipmentBox.objects.create(
            shipment=shipment,
            qr_code=qr_code[:256],
            external_box_id=f"FBN-B{uuid4().hex[:8].upper()}",
            is_manual=True,
        )
        shipment.pilot_revision += 1
        shipment.save(update_fields=("pilot_revision", "updated_at"))
        _event(shipment, "add_box", actor, metadata={"box_id": box.id})
    return box


def remove_box(*, shipment_id: int, box_id: int, actor=None) -> None:
    with transaction.atomic():
        shipment = WmsNewShipment.objects.select_for_update().get(pk=shipment_id)
        if shipment.status not in (WmsNewShipment.STATUS_NEW, WmsNewShipment.STATUS_CHECKING):
            raise ShipmentOperationError("Короб можно удалить только до завершения проверки.")
        box = WmsNewShipmentBox.objects.select_for_update().get(pk=box_id, shipment=shipment)
        if box.shipment_orders.exists():
            raise ShipmentOperationError("Сначала переместите заказы из короба.")
        external_id = box.external_box_id or box.id
        box.delete()
        shipment.pilot_revision += 1
        shipment.save(update_fields=("pilot_revision", "updated_at"))
        _event(shipment, "remove_box", actor, metadata={"box": str(external_id)})


def verify_order(
    *, shipment_id: int, shipment_order_id: int, box_id: int | None = None,
    in_supply: bool | None = None, actor=None
) -> WmsNewShipmentOrder:
    with transaction.atomic():
        shipment = WmsNewShipment.objects.select_for_update().get(pk=shipment_id)
        if shipment.status not in (WmsNewShipment.STATUS_NEW, WmsNewShipment.STATUS_CHECKING):
            raise ShipmentOperationError("Проверка заказов в этом статусе недоступна.")
        link = WmsNewShipmentOrder.objects.select_for_update().get(
            pk=shipment_order_id, shipment=shipment
        )
        if box_id:
            try:
                link.box = shipment.boxes.get(pk=box_id)
            except WmsNewShipmentBox.DoesNotExist as exc:
                raise ShipmentOperationError("Короб этой отгрузки не найден.") from exc
        link.verification_status = WmsNewShipmentOrder.VERIFY_CHECKED
        if in_supply is not None:
            link.in_supply = bool(in_supply)
            link.marketplace_state = "in_supply" if in_supply else "not_in_supply"
        link.verified_by = actor
        link.verified_at = timezone.now()
        link.save()
        before = {"status": shipment.status, "pilot_revision": shipment.pilot_revision}
        shipment.status = WmsNewShipment.STATUS_CHECKING
        shipment.pilot_revision += 1
        shipment.save(update_fields=("status", "pilot_revision", "updated_at"))
        _event(shipment, "verify_order", actor, before, {"order_id": link.order_id, "box_id": box_id})
    return link


def update_order_check(
    *, shipment_id: int, shipment_order_id: int, action: str,
    box_id: int | None = None, actor=None
) -> WmsNewShipmentOrder:
    if action == "verify":
        return verify_order(
            shipment_id=shipment_id,
            shipment_order_id=shipment_order_id,
            box_id=box_id,
            actor=actor,
        )
    if action not in {"unverify", "error", "move"}:
        raise ShipmentOperationError("Неизвестное действие проверки заказа.")
    with transaction.atomic():
        shipment = WmsNewShipment.objects.select_for_update().get(pk=shipment_id)
        if shipment.status not in (WmsNewShipment.STATUS_NEW, WmsNewShipment.STATUS_CHECKING):
            raise ShipmentOperationError("Изменять проверку можно только до ее завершения.")
        link = WmsNewShipmentOrder.objects.select_for_update().get(
            pk=shipment_order_id,
            shipment=shipment,
        )
        if box_id:
            try:
                link.box = shipment.boxes.get(pk=box_id)
            except WmsNewShipmentBox.DoesNotExist as exc:
                raise ShipmentOperationError("Короб этой отгрузки не найден.") from exc
        elif action == "move":
            link.box = None
        if action == "unverify":
            link.verification_status = WmsNewShipmentOrder.VERIFY_NOT_CHECKED
            link.verified_by = None
            link.verified_at = None
        elif action == "error":
            link.verification_status = WmsNewShipmentOrder.VERIFY_ERROR
            link.verified_by = actor
            link.verified_at = timezone.now()
        link.save()
        shipment.status = WmsNewShipment.STATUS_CHECKING
        shipment.pilot_revision += 1
        shipment.save(update_fields=("status", "pilot_revision", "updated_at"))
        _event(
            shipment,
            f"order_{action}",
            actor,
            metadata={"order_id": link.order_id, "box_id": link.box_id},
        )
        return link


def transition_shipment(*, shipment_id: int, action: str, actor=None) -> WmsNewShipment:
    transitions = {
        "start_check": ({WmsNewShipment.STATUS_NEW}, WmsNewShipment.STATUS_CHECKING),
        "finish_check": ({WmsNewShipment.STATUS_NEW, WmsNewShipment.STATUS_CHECKING}, WmsNewShipment.STATUS_CHECKED),
        "send": ({WmsNewShipment.STATUS_CHECKED}, WmsNewShipment.STATUS_IN_TRANSIT),
        "accept": ({WmsNewShipment.STATUS_IN_TRANSIT}, WmsNewShipment.STATUS_ACCEPTED),
        "reject": ({WmsNewShipment.STATUS_IN_TRANSIT}, WmsNewShipment.STATUS_REJECTED),
        "partial": ({WmsNewShipment.STATUS_IN_TRANSIT}, WmsNewShipment.STATUS_PARTIAL),
    }
    rule = transitions.get(action)
    if rule is None:
        raise ShipmentOperationError("Неизвестное действие с отгрузкой.")
    with transaction.atomic():
        shipment = WmsNewShipment.objects.select_for_update().get(pk=shipment_id)
        allowed, target_status = rule
        if shipment.status not in allowed:
            raise ShipmentOperationError(
                f"Действие недоступно в статусе «{shipment.get_status_display()}»."
            )
        links = list(shipment.shipment_orders.select_for_update().select_related("order"))
        if action in {"start_check", "finish_check", "send"} and not links:
            raise ShipmentOperationError("Сначала добавьте в отгрузку хотя бы один заказ.")
        if action == "finish_check" and any(
            link.verification_status != WmsNewShipmentOrder.VERIFY_CHECKED for link in links
        ):
            raise ShipmentOperationError("Сначала проверьте все заказы отгрузки.")
        if action == "send" and any(not link.in_supply for link in links):
            raise ShipmentOperationError(
                "В отгрузке есть заказы, не подтвержденные в поставке маркетплейса."
            )
        before = {"status": shipment.status, "pilot_revision": shipment.pilot_revision}
        shipment.status = target_status
        shipment.pilot_revision += 1
        now = timezone.now()
        if target_status == WmsNewShipment.STATUS_CHECKED:
            shipment.checked_at = now
            shipment.checked_by = actor
        if target_status == WmsNewShipment.STATUS_IN_TRANSIT:
            shipment.dispatched_at = now
            shipment.dispatched_by = actor
        if target_status in {
            WmsNewShipment.STATUS_ACCEPTED,
            WmsNewShipment.STATUS_REJECTED,
            WmsNewShipment.STATUS_PARTIAL,
        }:
            shipment.accepted_at = now
        shipment.save()
        order_status = {
            WmsNewShipment.STATUS_IN_TRANSIT: WmsNewOrder.STATUS_HANDED_OVER,
            WmsNewShipment.STATUS_ACCEPTED: WmsNewOrder.STATUS_DONE,
            WmsNewShipment.STATUS_REJECTED: WmsNewOrder.STATUS_EXCEPTION,
            WmsNewShipment.STATUS_PARTIAL: WmsNewOrder.STATUS_EXCEPTION,
        }.get(target_status)
        if order_status:
            for link in links:
                order = link.order
                order.status = order_status
                order.pilot_revision += 1
                order.save(update_fields=("status", "pilot_revision", "updated_at"))
        _event(shipment, action, actor, before)
    return shipment


def send_all_checked(*, actor=None) -> int:
    ids = list(
        WmsNewShipment.objects.filter(status=WmsNewShipment.STATUS_CHECKED).values_list("id", flat=True)
    )
    for shipment_id in ids:
        transition_shipment(shipment_id=shipment_id, action="send", actor=actor)
    return len(ids)


def add_service(
    *, shipment_id: int, name: str, unit_price, quantity, actor=None
) -> WmsNewShipmentService:
    name = str(name or "").strip()
    if not name:
        raise ShipmentOperationError("Укажите услугу.")
    with transaction.atomic():
        shipment = WmsNewShipment.objects.select_for_update().get(pk=shipment_id)
        if shipment.tariff_finalized:
            raise ShipmentOperationError("Тарификация этой отгрузки уже завершена.")
        service = WmsNewShipmentService.objects.create(
            shipment=shipment,
            name=name[:255],
            unit_price=_decimal(unit_price, "Цена за единицу"),
            quantity=_decimal(quantity, "Количество"),
            created_by=actor,
        )
        shipment.pilot_revision += 1
        shipment.save(update_fields=("pilot_revision", "updated_at"))
        _event(shipment, "add_service", actor, metadata={"service_id": service.id})
    return service


def remove_service(*, shipment_id: int, service_id: int, actor=None) -> None:
    with transaction.atomic():
        shipment = WmsNewShipment.objects.select_for_update().get(pk=shipment_id)
        if shipment.tariff_finalized:
            raise ShipmentOperationError("Тарификация этой отгрузки уже завершена.")
        service = WmsNewShipmentService.objects.select_for_update().get(
            pk=service_id,
            shipment=shipment,
        )
        title = service.name
        service.delete()
        shipment.pilot_revision += 1
        shipment.save(update_fields=("pilot_revision", "updated_at"))
        _event(shipment, "remove_service", actor, metadata={"service": title})


def finalize_tariff(*, shipment_id: int, actor=None) -> WmsNewShipment:
    with transaction.atomic():
        shipment = WmsNewShipment.objects.select_for_update().get(pk=shipment_id)
        if shipment.tariff_finalized:
            return shipment
        shipment.tariff_finalized = True
        shipment.pilot_revision += 1
        shipment.save(update_fields=("tariff_finalized", "pilot_revision", "updated_at"))
        total = sum((service.total for service in shipment.services.all()), Decimal("0"))
        _event(shipment, "finalize_tariff", actor, metadata={"services": shipment.services.count(), "total": str(total)})
    return shipment
