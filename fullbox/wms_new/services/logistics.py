from __future__ import annotations

from uuid import uuid4

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import (
    WmsNewEvent,
    WmsNewLogisticsManifest,
    WmsNewLogisticsOrder,
    WmsNewLogisticsPackage,
    WmsNewLogisticsPackageOrder,
    WmsNewLogisticsRouteRule,
    WmsNewShipment,
    WmsNewShipmentOrder,
)


class LogisticsOperationError(ValueError):
    pass


def _event(entity_type: str, entity_id: int, action: str, actor=None, **after) -> None:
    WmsNewEvent.objects.create(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor=actor,
        after=after,
    )


def _clean_positive(value, label: str, *, allow_zero: bool = True) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise LogisticsOperationError(f"{label} должно быть целым числом.") from exc
    if result < 0 or (not allow_zero and result == 0):
        raise LogisticsOperationError(f"{label} указано неверно.")
    return result


@transaction.atomic
def accept_order(*, number: str, actor=None) -> WmsNewLogisticsOrder:
    clean_number = str(number or "").strip()
    if not clean_number:
        raise LogisticsOperationError("Отсканируйте номер отправления.")
    if WmsNewLogisticsOrder.objects.filter(number=clean_number).exists():
        raise LogisticsOperationError("Такое отправление уже принято.")
    item = WmsNewLogisticsOrder.objects.create(
        number=clean_number,
        tracking_number=clean_number,
        source_type=WmsNewLogisticsOrder.SOURCE_MANUAL,
        source_label="Ручная",
        status=WmsNewLogisticsOrder.STATUS_UNMEASURED,
        delivery_status="Принято",
        last_checked_at=timezone.now(),
        is_manual=True,
        pilot_revision=1,
        created_by=actor,
    )
    _event("logistics_order", item.id, "accept", actor, number=item.number)
    return item


@transaction.atomic
def import_fbs_shipments(
    *,
    shipment_ids: list[int],
    order_ids: list[int] | None = None,
    measurements: dict[int, dict] | None = None,
    actor=None,
) -> list[WmsNewLogisticsOrder]:
    ids = sorted({int(value) for value in shipment_ids if int(value) > 0})
    if not ids:
        raise LogisticsOperationError("Выберите хотя бы одну FBS-отгрузку.")
    shipments = list(
        WmsNewShipment.objects.filter(id__in=ids)
        .select_related("agency")
        .prefetch_related("shipment_orders__order")
    )
    if len(shipments) != len(ids):
        raise LogisticsOperationError("Часть FBS-отгрузок не найдена.")
    selected_order_ids = None
    if order_ids is not None:
        selected_order_ids = {int(value) for value in order_ids if int(value) > 0}
        if not selected_order_ids:
            raise LogisticsOperationError("Выберите хотя бы одно отправление.")
        available_order_ids = {
            link.order_id
            for shipment in shipments
            for link in shipment.shipment_orders.all()
        }
        if not selected_order_ids.issubset(available_order_ids):
            raise LogisticsOperationError("Часть выбранных отправлений не входит в отгрузку.")
    measurements = measurements or {}
    created_rows = []
    for shipment in shipments:
        for link in shipment.shipment_orders.select_related("order"):
            order = link.order
            if selected_order_ids is not None and order.id not in selected_order_ids:
                continue
            number = order.external_order_id
            values = measurements.get(order.id, {})
            weight_g = _clean_positive(values.get("weight_g"), "Вес")
            width_mm = _clean_positive(values.get("width_mm"), "Ширина")
            height_mm = _clean_positive(values.get("height_mm"), "Высота")
            depth_mm = _clean_positive(values.get("depth_mm"), "Глубина")
            if weight_g <= 0:
                status = WmsNewLogisticsOrder.STATUS_UNMEASURED
            elif not all((width_mm, height_mm, depth_mm)):
                status = WmsNewLogisticsOrder.STATUS_NO_DIMENSIONS
            else:
                status = WmsNewLogisticsOrder.STATUS_OK
            defaults = {
                "source_fbs_shipment": shipment,
                "agency": order.agency,
                "tracking_number": order.tracking_number,
                "source_type": WmsNewLogisticsOrder.SOURCE_FBS,
                "source_label": order.delivery_type or shipment.delivery_type or "FBS",
                "item_count": sum(item.quantity for item in order.items.all()),
                "weight_g": weight_g,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "depth_mm": depth_mm,
                "status": status,
                "delivery_status": order.get_status_display(),
                "source_updated_at": order.updated_at,
                "last_checked_at": timezone.now(),
                "last_synced_at": timezone.now(),
                "source_snapshot": {"shipment_id": shipment.id, "shipment_order_id": link.id},
                "created_by": actor,
            }
            item, created = WmsNewLogisticsOrder.objects.get_or_create(
                source_fbs_order=order,
                defaults={"number": number, **defaults},
            )
            if created:
                created_rows.append(item)
                _event(
                    "logistics_order",
                    item.id,
                    "import_fbs",
                    actor,
                    shipment_id=shipment.id,
                    order_id=order.id,
                )
    if not created_rows:
        raise LogisticsOperationError("Все отправления выбранных отгрузок уже загружены.")
    return created_rows


@transaction.atomic
def measure_order(
    *, order_id: int, weight_g=0, width_mm=0, height_mm=0, depth_mm=0, actor=None
) -> WmsNewLogisticsOrder:
    item = WmsNewLogisticsOrder.objects.select_for_update().get(pk=order_id)
    item.weight_g = _clean_positive(weight_g, "Вес")
    item.width_mm = _clean_positive(width_mm, "Ширина")
    item.height_mm = _clean_positive(height_mm, "Высота")
    item.depth_mm = _clean_positive(depth_mm, "Глубина")
    item.last_checked_at = timezone.now()
    if item.weight_g <= 0:
        item.status = WmsNewLogisticsOrder.STATUS_UNMEASURED
    elif not all((item.width_mm, item.height_mm, item.depth_mm)):
        item.status = WmsNewLogisticsOrder.STATUS_NO_DIMENSIONS
    else:
        item.status = WmsNewLogisticsOrder.STATUS_OK
        item.error_text = ""
    item.pilot_revision += 1
    item.save()
    _event(
        "logistics_order",
        item.id,
        "measure",
        actor,
        weight_g=item.weight_g,
        width_mm=item.width_mm,
        height_mm=item.height_mm,
        depth_mm=item.depth_mm,
        status=item.status,
    )
    return item


@transaction.atomic
def delete_orders(*, order_ids: list[int], actor=None) -> int:
    ids = sorted({int(value) for value in order_ids if int(value) > 0})
    rows = list(WmsNewLogisticsOrder.objects.select_for_update().filter(id__in=ids))
    if any(hasattr(item, "package_link") for item in rows):
        raise LogisticsOperationError("Сначала исключите отправление из грузоместа.")
    count = 0
    for item in rows:
        if not item.is_manual and item.source_type == WmsNewLogisticsOrder.SOURCE_SHIPPING:
            raise LogisticsOperationError("Синхронизированное отправление удалить нельзя.")
        _event("logistics_order", item.id, "delete", actor, number=item.number)
        item.delete()
        count += 1
    return count


@transaction.atomic
def create_package(*, carrier_code: str, delivery_service: str, warehouse_code: str, actor=None):
    if carrier_code not in dict(WmsNewLogisticsPackage.CARRIER_CHOICES):
        raise LogisticsOperationError("Выберите транспортную компанию.")
    service = str(delivery_service or "").strip()
    if not service:
        raise LogisticsOperationError("Выберите источник отправлений.")
    number = f"FBN-LG-{timezone.localdate():%y%m%d}-{uuid4().hex[:6].upper()}"
    item = WmsNewLogisticsPackage.objects.create(
        number=number,
        carrier_code=carrier_code,
        delivery_service=service,
        warehouse_code=str(warehouse_code or "").strip(),
        created_by=actor,
    )
    _event("logistics_package", item.id, "create", actor, number=item.number)
    return item


def _recalculate_package(item: WmsNewLogisticsPackage) -> None:
    total = item.package_orders.aggregate(weight=Sum("order__weight_g"))["weight"] or 0
    item.weight_g = int(total)
    item.save(update_fields=("weight_g", "updated_at"))


def _recalculate_manifest(item: WmsNewLogisticsManifest) -> None:
    packages = item.packages.all()
    item.total_weight_g = int(packages.aggregate(weight=Sum("weight_g"))["weight"] or 0)
    item.total_items = sum(
        order.item_count
        for order in WmsNewLogisticsOrder.objects.filter(package_link__package__manifest=item)
    )
    item.save(update_fields=("total_weight_g", "total_items", "updated_at"))


@transaction.atomic
def add_orders_to_package(*, package_id: int, order_ids: list[int], actor=None) -> int:
    package = WmsNewLogisticsPackage.objects.select_for_update().get(pk=package_id)
    if package.status != WmsNewLogisticsPackage.STATUS_NEW:
        raise LogisticsOperationError("Отправленное грузоместо менять нельзя.")
    ids = sorted({int(value) for value in order_ids if int(value) > 0})
    orders = list(
        WmsNewLogisticsOrder.objects.select_for_update(of=("self",))
        .filter(id__in=ids)
        .exclude(package_link__isnull=False)
    )
    if len(orders) != len(ids):
        raise LogisticsOperationError("Часть отправлений уже находится в грузоместе.")
    for order in orders:
        WmsNewLogisticsPackageOrder.objects.create(package=package, order=order)
    _recalculate_package(package)
    _event("logistics_package", package.id, "add_orders", actor, order_ids=ids)
    return len(orders)


@transaction.atomic
def remove_orders_from_package(*, package_id: int, order_ids: list[int], actor=None) -> int:
    package = WmsNewLogisticsPackage.objects.select_for_update().get(pk=package_id)
    if package.status != WmsNewLogisticsPackage.STATUS_NEW:
        raise LogisticsOperationError("Отправленное грузоместо менять нельзя.")
    ids = sorted({int(value) for value in order_ids if int(value) > 0})
    links = list(
        WmsNewLogisticsPackageOrder.objects.select_for_update().filter(
            package=package,
            order_id__in=ids,
        )
    )
    if not ids or len(links) != len(ids):
        raise LogisticsOperationError("Выберите отправления этого грузоместа.")
    WmsNewLogisticsPackageOrder.objects.filter(id__in=[row.id for row in links]).delete()
    _recalculate_package(package)
    if package.manifest_id:
        _recalculate_manifest(package.manifest)
    _event("logistics_package", package.id, "remove_orders", actor, order_ids=ids)
    return len(links)


@transaction.atomic
def update_packages(*, package_ids: list[int], action: str, manifest_id=None, actor=None) -> int:
    ids = sorted({int(value) for value in package_ids if int(value) > 0})
    rows = list(WmsNewLogisticsPackage.objects.select_for_update().filter(id__in=ids))
    if len(rows) != len(ids) or not rows:
        raise LogisticsOperationError("Выберите грузоместа.")
    if action == "send":
        for item in rows:
            if not item.package_orders.exists():
                raise LogisticsOperationError(f"Грузоместо {item.number} пустое.")
            item.status = WmsNewLogisticsPackage.STATUS_SENT
            item.save(update_fields=("status", "updated_at"))
            WmsNewLogisticsOrder.objects.filter(
                package_link__package=item
            ).update(delivery_status="Передано в транспортную компанию")
            _event("logistics_package", item.id, "send", actor, status=item.status)
        return len(rows)
    if action == "manifest":
        manifest = WmsNewLogisticsManifest.objects.select_for_update().get(pk=int(manifest_id or 0))
        if manifest.status != WmsNewLogisticsManifest.STATUS_NEW:
            raise LogisticsOperationError("Добавлять грузоместа можно только в новый рейс.")
        for item in rows:
            if item.status != WmsNewLogisticsPackage.STATUS_NEW:
                raise LogisticsOperationError("Отправленное грузоместо нельзя перенести в рейс.")
            item.manifest = manifest
            item.save(update_fields=("manifest", "updated_at"))
            _event("logistics_package", item.id, "assign_manifest", actor, manifest_id=manifest.id)
        _recalculate_manifest(manifest)
        return len(rows)
    if action == "delete":
        for item in rows:
            if item.status != WmsNewLogisticsPackage.STATUS_NEW:
                raise LogisticsOperationError("Отправленное грузоместо удалить нельзя.")
            _event("logistics_package", item.id, "delete", actor, number=item.number)
            item.delete()
        return len(rows)
    raise LogisticsOperationError("Неизвестное действие с грузоместами.")


@transaction.atomic
def transition_manifest(*, manifest_id: int, action: str, actor=None) -> WmsNewLogisticsManifest:
    item = WmsNewLogisticsManifest.objects.select_for_update().get(pk=manifest_id)
    transitions = {
        "send": ({WmsNewLogisticsManifest.STATUS_NEW}, WmsNewLogisticsManifest.STATUS_SENT),
        "complete": ({WmsNewLogisticsManifest.STATUS_SENT}, WmsNewLogisticsManifest.STATUS_COMPLETED),
        "cancel": ({WmsNewLogisticsManifest.STATUS_NEW}, WmsNewLogisticsManifest.STATUS_CANCELLED),
    }
    rule = transitions.get(action)
    if rule is None:
        raise LogisticsOperationError("Неизвестное действие с рейсом.")
    allowed, target = rule
    if item.status not in allowed:
        raise LogisticsOperationError("Действие недоступно в текущем статусе рейса.")
    packages = list(item.packages.select_for_update().prefetch_related("package_orders"))
    if action == "send":
        if not packages:
            raise LogisticsOperationError("Добавьте в рейс хотя бы одно грузоместо.")
        if any(not package.package_orders.exists() for package in packages):
            raise LogisticsOperationError("В рейсе есть пустое грузоместо.")
        for package in packages:
            package.status = WmsNewLogisticsPackage.STATUS_SENT
            package.save(update_fields=("status", "updated_at"))
            WmsNewLogisticsOrder.objects.filter(
                package_link__package=package
            ).update(delivery_status="В рейсе")
    if action == "complete":
        WmsNewLogisticsOrder.objects.filter(
            package_link__package__manifest=item
        ).update(delivery_status="Доставлено рейсом")
    if action == "cancel":
        item.packages.update(manifest=None)
    item.status = target
    item.pilot_revision += 1
    item.save(update_fields=("status", "pilot_revision", "updated_at"))
    _event("logistics_manifest", item.id, action, actor, status=item.status)
    return item


@transaction.atomic
def create_manifest(
    *, name: str, total_weight_g=0, departure_airport: str = "",
    destination_airport: str = "", expected_arrival_date=None, actor=None
) -> WmsNewLogisticsManifest:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise LogisticsOperationError("Укажите название рейса.")
    item = WmsNewLogisticsManifest.objects.create(
        name=clean_name,
        total_weight_g=_clean_positive(total_weight_g, "Вес"),
        departure_airport=str(departure_airport or "").strip(),
        destination_airport=str(destination_airport or "").strip(),
        expected_arrival_date=expected_arrival_date,
        is_manual=True,
        pilot_revision=1,
        created_by=actor,
    )
    _event("logistics_manifest", item.id, "create", actor, name=item.name)
    return item


@transaction.atomic
def save_route_rules(*, rows: list[dict], actor=None) -> int:
    changed = 0
    international = {"", WmsNewLogisticsPackage.CARRIER_GBS, WmsNewLogisticsPackage.CARRIER_MANUAL}
    local = {"", WmsNewLogisticsPackage.CARRIER_CDEK, WmsNewLogisticsPackage.CARRIER_RUSSIAN_POST}
    for row in rows:
        service = str(row.get("delivery_service") or "").strip()
        intl = str(row.get("international_carrier") or "").strip()
        local_code = str(row.get("local_carrier") or "").strip()
        if not service or intl not in international or local_code not in local:
            raise LogisticsOperationError("Проверьте настройки маршрута.")
        item, _ = WmsNewLogisticsRouteRule.objects.select_for_update().get_or_create(
            delivery_service=service
        )
        item.international_carrier = intl
        item.local_carrier = local_code
        item.is_active = bool(row.get("is_active"))
        item.updated_by = actor
        item.save()
        _event("logistics_route", item.id, "update", actor, service=service)
        changed += 1
    return changed
