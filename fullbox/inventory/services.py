from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from sklad.models import WarehouseStockSnapshot

from .models import Inventory, InventoryLine, InventoryLocation


def _snapshot_queryset(inventory: Inventory):
    queryset = (
        WarehouseStockSnapshot.objects.filter(
            is_archived=False,
            qty__gt=0,
            location__isnull=False,
            location__is_active=True,
        )
        .select_related("agency", "sku_ref", "location")
        .order_by("location_id", "agency_id", "sku_code", "size", "barcode", "id")
    )
    if inventory.inventory_type == Inventory.TYPE_PARTNER:
        if inventory.agency_id is None:
            raise ValidationError("Для инвентаризации по партнеру партнер не выбран.")
        queryset = queryset.filter(agency_id=inventory.agency_id)
    elif inventory.inventory_type == Inventory.TYPE_GOODS:
        if inventory.sku_id is None:
            raise ValidationError("Для инвентаризации по товару товар не выбран.")
        sku = inventory.sku
        queryset = queryset.filter(
            Q(sku_ref_id=inventory.sku_id)
            | Q(agency_id=sku.agency_id, sku_code=sku.sku_code)
        )
    elif inventory.inventory_type == Inventory.TYPE_PLACES:
        location_ids = list(inventory.scope_locations.values_list("location_id", flat=True))
        if not location_ids:
            raise ValidationError("Не выбраны места инвентаризации.")
        queryset = queryset.filter(location_id__in=location_ids)
    return queryset


def _line_key(snapshot: WarehouseStockSnapshot) -> tuple:
    return (
        snapshot.location_id,
        snapshot.agency_id,
        snapshot.sku_ref_id,
        str(snapshot.sku_code or "").strip(),
        str(snapshot.name or "").strip(),
        str(snapshot.size or "").strip(),
        str(snapshot.barcode or "").strip(),
        str(snapshot.goods_type or "").strip(),
    )


@transaction.atomic
def transfer_to_work(inventory: Inventory, *, requested_by=None) -> Inventory:
    inventory = Inventory.objects.select_for_update().get(pk=inventory.pk)
    if inventory.status != Inventory.STATUS_CREATED:
        raise ValidationError("Передать в работу можно только созданную инвентаризацию.")

    snapshots = _snapshot_queryset(inventory)
    if inventory.inventory_type != Inventory.TYPE_PLACES:
        location_ids = list(snapshots.values_list("location_id", flat=True).distinct())
        if not location_ids:
            raise ValidationError("По выбранному контуру нет складских остатков для пересчета.")
        InventoryLocation.objects.bulk_create(
            [InventoryLocation(inventory=inventory, location_id=location_id) for location_id in location_ids],
            ignore_conflicts=True,
        )

    scope_rows = list(inventory.scope_locations.select_related("location").order_by("location_id"))
    if not scope_rows:
        raise ValidationError("Не определены места инвентаризации.")

    aggregates: dict[tuple, dict] = {}
    for snapshot in snapshots.iterator(chunk_size=1000):
        key = _line_key(snapshot)
        row = aggregates.setdefault(
            key,
            {
                "planned_qty": 0,
                "source_snapshot_ids": [],
            },
        )
        row["planned_qty"] += int(snapshot.qty or 0)
        row["source_snapshot_ids"].append(
            {"id": snapshot.id, "version": int(snapshot.snapshot_version or 1)}
        )

    inventory.lines.all().delete()
    lines = []
    planned_by_location = defaultdict(int)
    for key, values in aggregates.items():
        (
            location_id,
            agency_id,
            sku_ref_id,
            sku_code,
            name,
            size,
            barcode,
            goods_type,
        ) = key
        planned_qty = int(values["planned_qty"] or 0)
        planned_by_location[location_id] += planned_qty
        lines.append(
            InventoryLine(
                inventory=inventory,
                location_id=location_id,
                agency_id=agency_id,
                sku_ref_id=sku_ref_id,
                sku_code=sku_code,
                name=name,
                size=size,
                barcode=barcode,
                goods_type=goods_type,
                planned_qty=planned_qty,
                source_snapshot_ids=values["source_snapshot_ids"],
            )
        )
    if lines:
        InventoryLine.objects.bulk_create(lines, batch_size=500)

    for scope in scope_rows:
        scope.planned_qty = planned_by_location.get(scope.location_id, 0)
    InventoryLocation.objects.bulk_update(scope_rows, ["planned_qty"], batch_size=500)

    from reachtruck_inventory.models import InventoryTask

    InventoryTask.objects.bulk_create(
        [
            InventoryTask(
                inventory=inventory,
                scope_location=scope,
                location=scope.location,
            )
            for scope in scope_rows
        ],
        ignore_conflicts=True,
    )
    inventory.status = Inventory.STATUS_PENDING
    inventory.transferred_at = timezone.now()
    inventory.save(update_fields=["status", "transferred_at", "updated_at"])
    return inventory


@transaction.atomic
def cancel_inventory(inventory: Inventory) -> Inventory:
    inventory = Inventory.objects.select_for_update().get(pk=inventory.pk)
    if inventory.status == Inventory.STATUS_COMPLETED:
        raise ValidationError("Завершенную инвентаризацию отменить нельзя.")
    if inventory.status == Inventory.STATUS_CANCELED:
        return inventory

    from reachtruck_inventory.models import InventoryTask

    now = timezone.now()
    inventory.execution_tasks.exclude(status=InventoryTask.STATUS_COMPLETED).update(
        status=InventoryTask.STATUS_CANCELED,
        lease_expires_at=None,
        canceled_at=now,
        updated_at=now,
    )
    inventory.status = Inventory.STATUS_CANCELED
    inventory.canceled_at = now
    inventory.save(update_fields=["status", "canceled_at", "updated_at"])
    return inventory
