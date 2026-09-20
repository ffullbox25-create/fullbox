"""План/факт по направлениям для карточки приёмки и менеджера."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import ReceivingDistributionAllocation, ReceivingDistributionPlan


@dataclass
class AllocProgress:
    sku_code: str
    name: str
    qty_plan: int
    qty_accepted: int

    @property
    def qty_remaining(self) -> int:
        return max(int(self.qty_plan) - int(self.qty_accepted), 0)


@dataclass
class DirectionProgress:
    id: int
    title: str
    kind: str
    status: str
    status_label: str
    marketplace_name: str
    destination_warehouse: str
    supply_number: str
    shipping_number: str
    shipping_pk: int | None
    shipping_status_label: str
    qty_plan: int
    qty_accepted: int
    allocations: list[AllocProgress] = field(default_factory=list)

    @property
    def qty_remaining(self) -> int:
        return max(int(self.qty_plan) - int(self.qty_accepted), 0)


@dataclass
class ItemProgress:
    sku_code: str
    name: str
    barcode: str
    qty_total: int
    qty_allocated: int
    qty_accepted: int
    by_direction: list[AllocProgress] = field(default_factory=list)


@dataclass
class DistributionPanel:
    plan_id: int
    receiving_order_id: str
    status: str
    status_label: str
    expected_units: int
    directions: list[DirectionProgress]
    items: list[ItemProgress]
    shipping_count: int
    storage_only_count: int


def build_distribution_panel(receiving_order_id: str) -> DistributionPanel | None:
    order_id = str(receiving_order_id or "").strip()
    if not order_id:
        return None
    plan = (
        ReceivingDistributionPlan.objects.filter(receiving_order_id=order_id)
        .prefetch_related(
            "directions__shipping_order",
            "directions__allocations__item",
            "items__allocations__direction",
        )
        .first()
    )
    if not plan:
        return None

    directions: list[DirectionProgress] = []
    for direction in plan.directions.all():
        allocs = []
        plan_qty = 0
        accepted_qty = 0
        for alloc in direction.allocations.select_related("item"):
            plan_qty += int(alloc.qty)
            accepted_qty += int(alloc.qty_accepted or 0)
            allocs.append(
                AllocProgress(
                    sku_code=alloc.item.sku_code,
                    name=alloc.item.name,
                    qty_plan=int(alloc.qty),
                    qty_accepted=int(alloc.qty_accepted or 0),
                )
            )
        so = direction.shipping_order
        ship_pk = None
        if direction.kind == direction.KIND_STORAGE:
            ship_label = "Без отгрузки (хранение)"
            ship_number = ""
        elif so:
            ship_number = so.number
            ship_pk = so.pk
            # Черновик до фактической приёмки показываем как «Ожидает приемки»
            if direction.status == direction.STATUS_AWAITING and so.status == so.STATUS_DRAFT:
                ship_label = "Ожидает приемки"
            elif direction.status == direction.STATUS_PARTIAL:
                ship_label = "Принято частично / резерв по факту"
            elif direction.status in {direction.STATUS_DONE, direction.STATUS_READY}:
                ship_label = so.get_status_display() if so.status != so.STATUS_DRAFT else "Готово к отгрузке"
            elif so.status == so.STATUS_DRAFT:
                ship_label = "Ожидает приемки"
            else:
                ship_label = so.get_status_display()
        else:
            ship_number = ""
            ship_label = "Отгрузка не создана"

        directions.append(
            DirectionProgress(
                id=direction.id,
                title=direction.title,
                kind=direction.kind,
                status=direction.status,
                status_label=direction.get_status_display(),
                marketplace_name=direction.marketplace_name,
                destination_warehouse=direction.destination_warehouse,
                supply_number=direction.supply_number,
                shipping_number=ship_number,
                shipping_pk=ship_pk,
                shipping_status_label=ship_label,
                qty_plan=plan_qty or int(direction.expected_units or 0),
                qty_accepted=accepted_qty,
                allocations=allocs,
            )
        )

    items: list[ItemProgress] = []
    for item in plan.items.all():
        by_dir = []
        accepted = 0
        allocated = 0
        for alloc in item.allocations.select_related("direction"):
            allocated += int(alloc.qty)
            accepted += int(alloc.qty_accepted or 0)
            by_dir.append(
                AllocProgress(
                    sku_code=item.sku_code,
                    name=alloc.direction.title,
                    qty_plan=int(alloc.qty),
                    qty_accepted=int(alloc.qty_accepted or 0),
                )
            )
        items.append(
            ItemProgress(
                sku_code=item.sku_code,
                name=item.name,
                barcode=item.barcode,
                qty_total=int(item.qty_total),
                qty_allocated=allocated,
                qty_accepted=accepted,
                by_direction=by_dir,
            )
        )

    return DistributionPanel(
        plan_id=plan.id,
        receiving_order_id=plan.receiving_order_id,
        status=plan.status,
        status_label=plan.get_status_display(),
        expected_units=int(plan.expected_units or 0),
        directions=directions,
        items=items,
        shipping_count=sum(1 for d in directions if d.shipping_number),
        storage_only_count=sum(1 for d in directions if d.kind == "storage_fullbox"),
    )
