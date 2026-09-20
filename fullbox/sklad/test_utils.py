from __future__ import annotations

from sku.models import Agency
from sklad.models import WarehouseContainer, WarehouseStockSnapshot
from sklad.services import WarehouseStateCode, WarehouseWritePathService


def create_warehouse_snapshot_row(
    *,
    agency: Agency,
    order_type: str = "receiving",
    order_id: str = "TEST",
    sku: str = "SKU-TEST",
    name: str = "Товар",
    size: str = "",
    barcode: str = "",
    marking_code: str = "",
    goods_type: str = "Оптовый",
    qty: int = 1,
    available_qty: int | None = None,
    processing_reserved_qty: int = 0,
    shipping_reserved_qty: int = 0,
    box_code: str = "",
    pallet_code: str = "PAL-TEST",
    zone: str = "OS",
    row: int = 1,
    section: int = 1,
    tier: int = 1,
    cell: int = 1,
    warehouse_state_code: str = WarehouseStateCode.STORED.value,
) -> WarehouseStockSnapshot:
    location = WarehouseWritePathService.ensure_location(
        warehouse_code="MSK",
        zone_code=str(zone or "OS").strip().upper(),
        row_no=int(row or 0),
        section_no=int(section or 0),
        tier_no=int(tier or 0),
        cell_no=int(cell or 0),
    )
    parent_container = None
    container = None
    if pallet_code:
        parent_container, _ = WarehouseContainer.objects.get_or_create(
            agency=agency,
            container_code=pallet_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_PALLET,
                "current_location": location,
                "source_context_type": order_type,
                "source_context_id": str(order_id),
            },
        )
        if parent_container.current_location_id != location.id:
            parent_container.current_location = location
            parent_container.save(update_fields=["current_location", "updated_at"])
    if box_code:
        container, _ = WarehouseContainer.objects.get_or_create(
            agency=agency,
            container_code=box_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_BOX,
                "parent_container": parent_container,
                "current_location": location,
                "source_context_type": order_type,
                "source_context_id": str(order_id),
            },
        )
        update_fields = []
        if parent_container and container.parent_container_id != parent_container.id:
            container.parent_container = parent_container
            update_fields.append("parent_container")
        if container.current_location_id != location.id:
            container.current_location = location
            update_fields.append("current_location")
        if update_fields:
            container.save(update_fields=[*update_fields, "updated_at"])
    elif parent_container:
        container = parent_container
    return WarehouseStockSnapshot.objects.create(
        agency=agency,
        source_context_type=order_type,
        source_context_id=str(order_id),
        sku_code=sku,
        name=name,
        size=size,
        barcode=barcode,
        marking_code=marking_code,
        goods_type=goods_type,
        qty=int(qty or 0),
        available_qty=int(qty if available_qty is None else available_qty),
        processing_reserved_qty=int(processing_reserved_qty or 0),
        shipping_reserved_qty=int(shipping_reserved_qty or 0),
        container=container,
        container_code=str(container.container_code if container else ""),
        parent_container=parent_container if container and container.container_type == WarehouseContainer.TYPE_BOX else None,
        location=location,
        zone_code=location.zone_code,
        zone_kind=location.zone_kind,
        warehouse_state_code=warehouse_state_code,
    )
