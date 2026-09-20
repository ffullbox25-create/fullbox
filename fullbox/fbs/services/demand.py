from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

from fbs.exceptions import FbsReplenishmentError
from fbs.flags import module_enabled
from fbs.models import (
    FbsBox,
    FbsOrder,
    FbsOrderItem,
    FbsPallet,
    FbsReplenishmentAllocation,
    FbsReplenishmentLine,
    FbsReplenishmentPlan,
    FbsReplenishmentPolicy,
    FbsStockBalance,
)
from sku.models import Agency, SKU, SKUBarcode
from sklad.models import WarehouseStockSnapshot

from ..barcode_aliases import normalize_barcode, sku_barcode_alias_map
from .replenishment import create_replenishment_plan
from .traceability import metadata_requirements


DEMAND_ORDER_STATUSES = (
    FbsOrder.STATUS_RECEIVED,
    FbsOrder.STATUS_AWAITING_STOCK,
    FbsOrder.STATUS_RESERVED,
    FbsOrder.STATUS_QUEUED_FOR_PICK,
)
OPEN_PLAN_STATUSES = (
    FbsReplenishmentPlan.STATUS_PROPOSED,
    FbsReplenishmentPlan.STATUS_CONFIRMED,
    FbsReplenishmentPlan.STATUS_IN_PROGRESS,
)
OPEN_LINE_STATUSES = (
    FbsReplenishmentLine.STATUS_PROPOSED,
    FbsReplenishmentLine.STATUS_RESERVED,
    FbsReplenishmentLine.STATUS_IN_PROGRESS,
)
OPEN_ALLOCATION_STATUSES = (
    FbsReplenishmentAllocation.STATUS_RESERVED,
    FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
)
ACTIVE_BOX_STATUSES = (FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)
ACTIVE_PALLET_STATUSES = (FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE)


@dataclass(frozen=True)
class FbsDemandRow:
    agency_id: int
    agency_name: str
    sku_id: int
    sku_code: str
    sku_name: str
    barcode: str
    ordered_qty: int
    fbs_qty: int
    open_plan_qty: int
    uncovered_qty: int
    general_available_qty: int
    proposed_qty: int
    general_shortage_qty: int
    destination_blocked_qty: int
    target_box_id: int | None
    target_box_code: str
    minimum_qty: int
    target_qty: int
    auto_suggest: bool
    marking_required: bool = False


@dataclass(frozen=True)
class FbsDemandAnalysis:
    rows: tuple[FbsDemandRow, ...]
    order_count: int
    unmapped_line_count: int
    unmapped_qty: int

    @property
    def ordered_qty(self) -> int:
        return sum(row.ordered_qty for row in self.rows) + self.unmapped_qty

    @property
    def uncovered_qty(self) -> int:
        return sum(row.uncovered_qty for row in self.rows)

    @property
    def proposed_qty(self) -> int:
        return sum(row.proposed_qty for row in self.rows)

    @property
    def blocked_qty(self) -> int:
        return sum(
            row.general_shortage_qty + row.destination_blocked_qty for row in self.rows
        ) + self.unmapped_qty


@dataclass(frozen=True)
class FbsPlanGenerationResult:
    analysis: FbsDemandAnalysis
    plans: tuple[FbsReplenishmentPlan, ...]
    skipped_agencies: tuple[str, ...]


def _require_module() -> None:
    if not module_enabled():
        raise FbsReplenishmentError("Модуль FBS выключен.")


def _barcode(value: str) -> str:
    return str(value or "").strip()


def _available_boxes(agency_ids: set[int]) -> dict[int, FbsBox]:
    result: dict[int, FbsBox] = {}
    boxes = (
        FbsBox.objects.filter(
            agency_id__in=agency_ids,
            status__in=ACTIVE_BOX_STATUSES,
            pallet__status__in=ACTIVE_PALLET_STATUSES,
            pallet__cell__is_active=True,
        )
        .select_related("pallet__cell")
        .order_by("agency_id", "status", "pallet__cell__cell_code", "box_code", "id")
    )
    for box in boxes:
        if box.agency_id != box.pallet.agency_id:
            continue
        result.setdefault(box.agency_id, box)
    return result


def analyze_replenishment_demand(*, agency: Agency | None = None) -> FbsDemandAnalysis:
    _require_module()
    items = (
        FbsOrderItem.objects.filter(order__internal_status__in=DEMAND_ORDER_STATUSES)
        .select_related("order__profile__agency", "sku")
        .order_by("order_id", "id")
    )
    if agency is not None:
        items = items.filter(order__profile__agency=agency)
    items = list(items)
    item_sku_ids = {item.sku_id for item in items if item.sku_id}
    item_barcodes = {
        _barcode(item.barcode) for item in items if _barcode(item.barcode)
    }
    valid_barcode_pairs = set(
        SKUBarcode.objects.filter(
            sku_id__in=item_sku_ids,
            value__in=item_barcodes,
        ).values_list("sku_id", "value")
    )
    barcode_aliases = sku_barcode_alias_map(valid_barcode_pairs)

    demand: dict[tuple[int, str], int] = defaultdict(int)
    marking_required_by_key: dict[tuple[int, str], bool] = defaultdict(bool)
    sku_by_key: dict[tuple[int, str], SKU] = {}
    agency_by_id: dict[int, Agency] = {}
    aliases_by_key: dict[tuple[int, str], tuple[str, ...]] = {}
    order_ids: set[int] = set()
    unmapped_line_count = 0
    unmapped_qty = 0
    for item in items:
        order_ids.add(item.order_id)
        item_agency = item.order.profile.agency
        qty = int(item.quantity or 0)
        item_barcode = _barcode(item.barcode)
        if (
            item.sku_id is None
            or not item_barcode
            or item.sku.agency_id != item_agency.id
            or item.sku.deleted
            or (item.sku_id, item_barcode) not in valid_barcode_pairs
        ):
            unmapped_line_count += 1
            unmapped_qty += qty
            continue
        aliases = barcode_aliases.get(
            (int(item.sku_id), normalize_barcode(item_barcode)),
            (item_barcode,),
        )
        key = (item_agency.id, aliases[0])
        mapped_sku = sku_by_key.get(key)
        if mapped_sku is not None and mapped_sku.id != item.sku_id:
            unmapped_line_count += 1
            unmapped_qty += qty
            continue
        demand[key] += qty
        marking_required_by_key[key] = (
            marking_required_by_key[key] or metadata_requirements(item).marking_required
        )
        sku_by_key[key] = item.sku
        agency_by_id[item_agency.id] = item_agency
        aliases_by_key[key] = aliases

    if not demand:
        return FbsDemandAnalysis(
            rows=(),
            order_count=len(order_ids),
            unmapped_line_count=unmapped_line_count,
            unmapped_qty=unmapped_qty,
        )

    agency_ids = {key[0] for key in demand}
    sku_ids = {sku.id for sku in sku_by_key.values()}
    barcodes = {
        barcode
        for key in demand
        for barcode in aliases_by_key[key]
        if barcode
    }
    demand_key_by_barcode = {
        (key[0], normalize_barcode(barcode)): key
        for key in demand
        for barcode in aliases_by_key[key]
    }
    policies = {
        (policy.agency_id, policy.sku_id): policy
        for policy in FbsReplenishmentPolicy.objects.filter(
            agency_id__in=agency_ids,
            sku_id__in=sku_ids,
            is_active=True,
        )
    }

    fbs_qty: dict[tuple[int, str], int] = defaultdict(int)
    balances = FbsStockBalance.objects.filter(
        agency_id__in=agency_ids,
        barcode__in=barcodes,
    ).values("agency_id", "barcode", "qty", "marking_code")
    for balance in balances:
        alias_key = (balance["agency_id"], normalize_barcode(balance["barcode"]))
        key = demand_key_by_barcode.get(alias_key)
        if key is not None and (
            not marking_required_by_key[key]
            or bool(str(balance["marking_code"] or "").strip())
        ):
            fbs_qty[key] += int(balance["qty"] or 0)

    open_plan_qty: dict[tuple[int, str], int] = defaultdict(int)
    proposed_plan_qty_by_alias: dict[tuple[int, str], int] = defaultdict(int)
    open_lines = FbsReplenishmentLine.objects.filter(
        plan__agency_id__in=agency_ids,
        plan__status__in=OPEN_PLAN_STATUSES,
        plan__mode=FbsReplenishmentPlan.MODE_ITEM,
        status__in=OPEN_LINE_STATUSES,
        barcode__in=barcodes,
    ).values(
        "plan__agency_id",
        "plan__status",
        "barcode",
        "qty_requested",
        "qty_moved",
    )
    for line in open_lines:
        alias_key = (
            line["plan__agency_id"],
            normalize_barcode(line["barcode"]),
        )
        key = demand_key_by_barcode.get(alias_key)
        if key is not None:
            remaining = max(
                int(line["qty_requested"] or 0) - int(line["qty_moved"] or 0),
                0,
            )
            open_plan_qty[key] += remaining
            if line["plan__status"] == FbsReplenishmentPlan.STATUS_PROPOSED:
                proposed_plan_qty_by_alias[alias_key] += remaining

    proposed_box_counts = Counter(
        FbsReplenishmentLine.objects.filter(
            plan__agency_id__in=agency_ids,
            plan__status=FbsReplenishmentPlan.STATUS_PROPOSED,
            plan__mode=FbsReplenishmentPlan.MODE_BOX,
            status=FbsReplenishmentLine.STATUS_PROPOSED,
            source_container__isnull=False,
        ).values_list("plan__agency_id", "source_container_id")
    )
    if proposed_box_counts:
        proposed_container_ids = {key[1] for key in proposed_box_counts}
        proposed_box_snapshots = WarehouseStockSnapshot.objects.filter(
            agency_id__in=agency_ids,
            container_id__in=proposed_container_ids,
            barcode__in=barcodes,
            is_archived=False,
            qty__gt=0,
        ).values("agency_id", "container_id", "barcode", "qty", "marking_code")
        for snapshot in proposed_box_snapshots:
            alias_key = (
                snapshot["agency_id"],
                normalize_barcode(snapshot["barcode"]),
            )
            key = demand_key_by_barcode.get(alias_key)
            multiplier = proposed_box_counts[(snapshot["agency_id"], snapshot["container_id"])]
            if (
                key is not None
                and multiplier
                and (
                    not marking_required_by_key[key]
                    or bool(str(snapshot["marking_code"] or "").strip())
                )
            ):
                qty = int(snapshot["qty"] or 0) * multiplier
                open_plan_qty[key] += qty
                proposed_plan_qty_by_alias[alias_key] += qty

    active_box_allocations = FbsReplenishmentAllocation.objects.filter(
        line__plan__agency_id__in=agency_ids,
        line__plan__status__in=(
            FbsReplenishmentPlan.STATUS_CONFIRMED,
            FbsReplenishmentPlan.STATUS_IN_PROGRESS,
        ),
        line__plan__mode=FbsReplenishmentPlan.MODE_BOX,
        status__in=OPEN_ALLOCATION_STATUSES,
        source_snapshot__barcode__in=barcodes,
    ).values(
        "line__plan__agency_id",
        "source_snapshot__barcode",
        "source_snapshot__marking_code",
        "qty_planned",
        "qty_moved",
    )
    for allocation in active_box_allocations:
        alias_key = (
            allocation["line__plan__agency_id"],
            normalize_barcode(allocation["source_snapshot__barcode"]),
        )
        key = demand_key_by_barcode.get(alias_key)
        if key is not None and (
            not marking_required_by_key[key]
            or bool(str(allocation["source_snapshot__marking_code"] or "").strip())
        ):
            open_plan_qty[key] += max(
                int(allocation["qty_planned"] or 0) - int(allocation["qty_moved"] or 0),
                0,
            )

    general_available_qty: dict[tuple[int, str], int] = defaultdict(int)
    snapshots = (
        WarehouseStockSnapshot.objects.filter(
            agency_id__in=agency_ids,
            barcode__in=barcodes,
            is_archived=False,
            is_in_vehicle=False,
            active_operation__isnull=True,
            available_qty__gt=0,
        )
        .exclude(
            zone_code__iexact=str(getattr(settings, "FBS_ZONE_CODE", "FBS") or "FBS")
        )
        .values("agency_id", "barcode", "available_qty", "marking_code")
    )
    for snapshot in snapshots:
        alias_key = (
            snapshot["agency_id"],
            normalize_barcode(snapshot["barcode"]),
        )
        key = demand_key_by_barcode.get(alias_key)
        if key is not None and (
            not marking_required_by_key[key]
            or bool(str(snapshot["marking_code"] or "").strip())
        ):
            general_available_qty[alias_key] += int(snapshot["available_qty"] or 0)

    boxes = _available_boxes(agency_ids)
    rows = []
    for key in sorted(
        demand,
        key=lambda item: (
            str(agency_by_id[item[0]]).casefold(),
            sku_by_key[item].sku_code.casefold(),
            item[1],
            item,
        ),
    ):
        item_agency = agency_by_id[key[0]]
        sku = sku_by_key[key]
        ordered = demand[key]
        current_fbs = fbs_qty[key]
        planned = open_plan_qty[key]
        policy = policies.get((key[0], sku.id))
        minimum_qty = int(policy.minimum_qty or 0) if policy else 0
        target_qty = int(policy.target_qty or 0) if policy else 0
        desired_qty = ordered
        if policy and current_fbs <= minimum_qty:
            desired_qty = max(desired_qty, target_qty)
        uncovered = max(desired_qty - current_fbs - planned, 0)
        available_by_alias = {
            alias: max(
                general_available_qty[(key[0], normalize_barcode(alias))]
                - proposed_plan_qty_by_alias[(key[0], normalize_barcode(alias))],
                0,
            )
            for alias in aliases_by_key[key]
        }
        general_available = sum(available_by_alias.values())
        source_barcode = max(
            aliases_by_key[key],
            key=lambda alias: available_by_alias[alias],
        )
        candidate_qty = min(uncovered, available_by_alias[source_barcode])
        target_box = boxes.get(item_agency.id)
        auto_suggest = bool(policy.auto_suggest) if policy else True
        proposed = candidate_qty if target_box is not None and auto_suggest else 0
        rows.append(
            FbsDemandRow(
                agency_id=item_agency.id,
                agency_name=str(item_agency),
                sku_id=sku.id,
                sku_code=str(sku.sku_code or ""),
                sku_name=str(sku.name or ""),
                barcode=source_barcode if candidate_qty > 0 else key[1],
                ordered_qty=ordered,
                fbs_qty=current_fbs,
                open_plan_qty=planned,
                uncovered_qty=uncovered,
                general_available_qty=general_available,
                proposed_qty=proposed,
                general_shortage_qty=max(uncovered - general_available, 0),
                destination_blocked_qty=(
                    min(uncovered, general_available) if target_box is None else 0
                ),
                target_box_id=target_box.id if target_box else None,
                target_box_code=target_box.box_code if target_box else "",
                minimum_qty=minimum_qty,
                target_qty=target_qty,
                auto_suggest=auto_suggest,
                marking_required=marking_required_by_key[key],
            )
        )
    return FbsDemandAnalysis(
        rows=tuple(rows),
        order_count=len(order_ids),
        unmapped_line_count=unmapped_line_count,
        unmapped_qty=unmapped_qty,
    )


@transaction.atomic
def generate_replenishment_plans(
    *,
    agency: Agency | None = None,
    requested_by=None,
    requested_by_role: str = "system",
) -> FbsPlanGenerationResult:
    _require_module()
    initial = analyze_replenishment_demand(agency=agency)
    agency_ids = sorted({row.agency_id for row in initial.rows})
    if agency is not None and agency.id not in agency_ids:
        agency_ids.append(agency.id)
    list(
        Agency.objects.select_for_update()
        .filter(id__in=agency_ids)
        .order_by("id")
    )
    analysis = analyze_replenishment_demand(agency=agency)

    plans = []
    skipped = []
    rows_by_agency: dict[int, list[FbsDemandRow]] = defaultdict(list)
    for row in analysis.rows:
        if row.uncovered_qty > 0:
            rows_by_agency[row.agency_id].append(row)

    for agency_id in sorted(rows_by_agency):
        rows = rows_by_agency[agency_id]
        plannable = [row for row in rows if row.proposed_qty > 0]
        if not plannable:
            if any(row.destination_blocked_qty > 0 for row in rows):
                skipped.append(f"{rows[0].agency_name}: нет активного FBS-короба")
            elif any(row.general_shortage_qty > 0 for row in rows):
                skipped.append(f"{rows[0].agency_name}: нет доступного общего остатка")
            continue
        target_box = FbsBox.objects.select_related("pallet__cell").get(
            pk=plannable[0].target_box_id
        )
        locked_agency = Agency.objects.get(pk=agency_id)
        sku_map = SKU.objects.in_bulk(row.sku_id for row in plannable)
        plan = create_replenishment_plan(
            agency=locked_agency,
            mode=FbsReplenishmentPlan.MODE_ITEM,
            target_pallet=target_box.pallet,
            target_box=target_box,
            lines=[
                {
                    "sku": sku_map[row.sku_id],
                    "barcode": row.barcode,
                    "qty": row.proposed_qty,
                }
                for row in plannable
            ],
            requested_by=requested_by,
            requested_by_role=requested_by_role,
            comment="Автоплан по открытым FBS-заказам.",
        )
        plans.append(plan)

    return FbsPlanGenerationResult(
        analysis=analysis,
        plans=tuple(plans),
        skipped_agencies=tuple(skipped),
    )
