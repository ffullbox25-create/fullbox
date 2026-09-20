from __future__ import annotations

from datetime import datetime, time
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from sku.models import Agency

from ..models import WmsNewEvent, WmsNewOrder, WmsNewOrderItem, WmsNewWave, WmsNewWaveOrder


class OrderOperationError(Exception):
    pass


def _event(order: WmsNewOrder, action: str, actor, before: dict, metadata=None) -> None:
    WmsNewEvent.objects.create(
        entity_type="order",
        entity_id=order.id,
        action=action,
        actor=actor,
        before=before,
        after={
            "status": order.status,
            "warehouse_code": order.warehouse_code,
            "pilot_revision": order.pilot_revision,
        },
        metadata=metadata or {},
    )


def create_manual_order(
    *,
    agency: Agency,
    external_order_id: str,
    cutoff_date,
    tracking_number: str = "",
    auto_tracking: bool = False,
    actor=None,
) -> WmsNewOrder:
    external_order_id = str(external_order_id or "").strip()
    if not external_order_id:
        raise OrderOperationError("Укажите номер заказа.")
    if WmsNewOrder.objects.filter(
        agency=agency, external_order_id=external_order_id
    ).exists():
        raise OrderOperationError("Заказ с таким номером уже существует в FBS-NEW.")
    if auto_tracking:
        tracking_number = f"FBN-{uuid4().hex[:12].upper()}"
    cutoff_at = timezone.make_aware(datetime.combine(cutoff_date, time(23, 59, 59)))
    with transaction.atomic():
        order = WmsNewOrder.objects.create(
            agency=agency,
            marketplace="courier",
            integration_name="Курьер",
            external_order_id=external_order_id,
            delivery_type="Курьер",
            tracking_number=str(tracking_number or "").strip(),
            status=WmsNewOrder.STATUS_NEW,
            ordered_at=timezone.now(),
            cutoff_at=cutoff_at,
            source_created_at=timezone.now(),
            last_synced_at=timezone.now(),
            availability_state="unavailable",
            is_manual=True,
            pilot_revision=1,
            created_by=actor,
        )
        _event(order, "create_manual", actor, {})
    return order


def launch_wave(*, order_ids: list[int], actor=None) -> WmsNewWave:
    allowed = {
        WmsNewOrder.STATUS_NEW,
        WmsNewOrder.STATUS_AWAITING_STOCK,
        WmsNewOrder.STATUS_RESERVED,
    }
    with transaction.atomic():
        orders = list(
            WmsNewOrder.objects.select_for_update()
            .filter(id__in=order_ids)
            .prefetch_related("items")
            .order_by("id")
        )
        if len(orders) != len(set(order_ids)):
            raise OrderOperationError("Часть выбранных заказов не найдена в FBS-NEW.")
        if not orders:
            raise OrderOperationError("Выберите хотя бы один заказ.")
        invalid = [order.external_order_id for order in orders if order.status not in allowed]
        if invalid:
            raise OrderOperationError(
                "Нельзя запустить волну для заказов в текущем статусе: " + ", ".join(invalid[:5])
            )
        from .inventory import InventoryOperationError, assert_orders_unlocked
        from .picking import PickingOperationError, allocate_orders_to_wave

        try:
            assert_orders_unlocked(orders)
        except InventoryOperationError as exc:
            raise OrderOperationError(str(exc)) from exc
        wave = WmsNewWave.objects.create(
            number=f"FBN-{timezone.localtime():%Y%m%d}-{uuid4().hex[:8].upper()}",
            agency_id=orders[0].agency_id if len({order.agency_id for order in orders}) == 1 else None,
            created_by=actor,
            is_manual=True,
            pilot_revision=1,
        )
        try:
            result = allocate_orders_to_wave(wave=wave, orders=orders, actor=actor)
        except PickingOperationError as exc:
            raise OrderOperationError(str(exc)) from exc
        if not result["processed_orders_count"]:
            wave.delete()
            reasons = "; ".join(
                item["reason"] for item in result.get("problematic_orders", ())[:3]
            )
            raise OrderOperationError(reasons or "Ни один заказ не добавлен в волну.")
        WmsNewEvent.objects.create(
            entity_type="wave",
            entity_id=wave.id,
            action="create",
            actor=actor,
            after={
                "number": wave.number,
                "order_ids": list(wave.wave_orders.values_list("order_id", flat=True)),
                "planned_units": wave.planned_units,
                "creation_result": result,
            },
        )
    return wave


def apply_bulk_action(
    *,
    order_ids: list[int],
    action: str,
    actor=None,
    warehouse_code: str = "",
) -> int:
    transitions = {
        "return_from_pick": WmsNewOrder.STATUS_NEW,
        "status_new": WmsNewOrder.STATUS_NEW,
        "status_done": WmsNewOrder.STATUS_DONE,
        "status_cancelled": WmsNewOrder.STATUS_CANCELLED,
    }
    with transaction.atomic():
        orders = list(WmsNewOrder.objects.select_for_update().filter(id__in=order_ids))
        if len(orders) != len(set(order_ids)):
            raise OrderOperationError("Часть выбранных заказов не найдена в FBS-NEW.")
        if action == "move_warehouse":
            warehouse_code = str(warehouse_code or "").strip().upper()
            if not warehouse_code:
                raise OrderOperationError("Укажите склад назначения.")
            for order in orders:
                before = {
                    "status": order.status,
                    "warehouse_code": order.warehouse_code,
                    "pilot_revision": order.pilot_revision,
                }
                order.warehouse_code = warehouse_code
                order.pilot_revision += 1
                order.save(update_fields=("warehouse_code", "pilot_revision", "updated_at"))
                _event(order, action, actor, before)
            return len(orders)
        target_status = transitions.get(action)
        if not target_status:
            raise OrderOperationError("Неизвестная операция FBS-NEW.")
        for order in orders:
            before = {"status": order.status, "pilot_revision": order.pilot_revision}
            order.status = target_status
            order.pilot_revision += 1
            order.save(update_fields=("status", "pilot_revision", "updated_at"))
            _event(order, action, actor, before)
        return len(orders)


def update_order(
    *,
    order_id: int,
    status: str,
    warehouse_code: str,
    actor=None,
) -> WmsNewOrder:
    if status not in dict(WmsNewOrder.STATUS_CHOICES):
        raise OrderOperationError("Неизвестный статус заказа FBS-NEW.")
    warehouse_code = str(warehouse_code or "").strip().upper()
    if not warehouse_code:
        raise OrderOperationError("Укажите код склада.")
    with transaction.atomic():
        try:
            order = WmsNewOrder.objects.select_for_update().get(pk=order_id)
        except WmsNewOrder.DoesNotExist as exc:
            raise OrderOperationError("Заказ FBS-NEW не найден.") from exc
        if status in {
            WmsNewOrder.STATUS_QUEUED,
            WmsNewOrder.STATUS_PICKING,
            WmsNewOrder.STATUS_PICKED,
            WmsNewOrder.STATUS_READY,
        }:
            from .inventory import InventoryOperationError, assert_orders_unlocked

            try:
                assert_orders_unlocked(
                    [order],
                    for_execution=status
                    in {
                        WmsNewOrder.STATUS_PICKING,
                        WmsNewOrder.STATUS_PICKED,
                        WmsNewOrder.STATUS_READY,
                    },
                )
            except InventoryOperationError as exc:
                raise OrderOperationError(str(exc)) from exc
        before = {
            "status": order.status,
            "warehouse_code": order.warehouse_code,
            "pilot_revision": order.pilot_revision,
        }
        order.status = status
        order.warehouse_code = warehouse_code
        order.pilot_revision += 1
        order.save(update_fields=("status", "warehouse_code", "pilot_revision", "updated_at"))
        _event(order, "update", actor, before)
    return order


def update_wave(
    *,
    wave_id: int,
    status: str = "",
    place_name: str | None = None,
    actor=None,
) -> WmsNewWave:
    if status and status not in dict(WmsNewWave.STATUS_CHOICES):
        raise OrderOperationError("Неизвестный статус волны FBS-NEW.")
    if status == WmsNewWave.STATUS_CANCELLED:
        from .picking import PickingOperationError, cancel_wave

        try:
            return cancel_wave(wave_id=wave_id, actor=actor)
        except PickingOperationError as exc:
            raise OrderOperationError(str(exc)) from exc
    if status:
        raise OrderOperationError(
            "Статус волны вычисляется по фактическим сканам. Используйте экран подбора."
        )
    with transaction.atomic():
        try:
            wave = WmsNewWave.objects.select_for_update().get(pk=wave_id)
        except WmsNewWave.DoesNotExist as exc:
            raise OrderOperationError("Волна FBS-NEW не найдена.") from exc
        if not wave.is_manual:
            raise OrderOperationError(
                "Импортированная волна доступна в FBS-NEW только для просмотра."
            )
        if place_name is None:
            return wave
        if wave.status != WmsNewWave.STATUS_QUEUED:
            raise OrderOperationError(
                "Место можно предварительно назначить только новой волне."
            )
        from .picking import PickingOperationError, resolve_collection_place

        normalized_place = str(place_name or "").strip()
        before = {
            "status": wave.status,
            "place": wave.cart_name or wave.workstation_name,
        }
        place_code = ""
        if normalized_place:
            try:
                place_code, normalized_place, place_type = resolve_collection_place(
                    normalized_place
                )
            except PickingOperationError as exc:
                raise OrderOperationError(str(exc)) from exc
            wave.cart_name = normalized_place if place_type == "cart" else ""
            wave.workstation_name = normalized_place if place_type == "workstation" else ""
        else:
            wave.cart_name = ""
            wave.workstation_name = ""
        snapshot = dict(wave.source_snapshot or {})
        if place_code:
            snapshot["collection_place_code"] = place_code
        else:
            snapshot.pop("collection_place_code", None)
        wave.source_snapshot = snapshot
        wave.pilot_revision += 1
        wave.save()
        WmsNewEvent.objects.create(
            entity_type="wave",
            entity_id=wave.id,
            action="assign_place",
            actor=actor,
            before=before,
            after={
                "status": wave.status,
                "place": wave.cart_name or wave.workstation_name,
                "place_code": place_code,
            },
        )
    return wave
