from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Sum

from fbs.models import (
    FbsOrderStockAllocation,
    FbsPickBatch,
    FbsPickTask,
    FbsStockBalance,
)
from sklad.models import WarehouseOperation


ACTIVE_ALLOCATION_STATUSES = (
    FbsOrderStockAllocation.STATUS_RESERVED,
    FbsOrderStockAllocation.STATUS_PICKING,
)
FBS_FREE_PALLET_CONTEXT = "fbs_free_relocation"
FBS_FREE_BOX_CONTEXT = "fbs_free_box"


@dataclass(frozen=True)
class RelocationReservationState:
    blockers: tuple[str, ...]
    queued_batch_ids: tuple[int, ...]


def _batch_is_untouched(batch: FbsPickBatch) -> bool:
    return not (
        batch.status != FbsPickBatch.STATUS_QUEUED
        or batch.assigned_to_id is not None
        or batch.workstation_id is not None
        or batch.cart_id is not None
        or batch.started_at is not None
        or batch.claimed_at is not None
        or batch.picking_completed_at is not None
        or batch.verification_assigned_to_id is not None
        or batch.verification_started_at is not None
        or batch.cart_released_at is not None
        or batch.completed_at is not None
        or batch.canceled_at is not None
        or int(batch.picked_qty or 0) > 0
    )


def relocation_reservation_state(
    balance_ids,
    *,
    lock: bool = False,
) -> RelocationReservationState:
    """Allow reservations to travel only while their wave is untouched.

    The allocation remains bound to the same FBS balance and therefore follows
    the box to its new physical location.  A claimed wave, a started task or a
    partially picked allocation is always a hard blocker.
    """

    normalized_balance_ids = sorted({int(value) for value in balance_ids if value})
    if not normalized_balance_ids:
        return RelocationReservationState(blockers=(), queued_batch_ids=())

    allocation_queryset = FbsOrderStockAllocation.objects.filter(
        balance_id__in=normalized_balance_ids,
        status__in=ACTIVE_ALLOCATION_STATUSES,
    ).order_by("id")
    if lock:
        allocation_queryset = allocation_queryset.select_for_update(of=("self",))
    allocations = list(allocation_queryset)

    # The allocation row is the shared lock with wave claiming.  Lock it before
    # reading the task: the claimant performs the same lock before it may turn
    # RESERVED into PICKING, while the move publishes its in-transit operation
    # before releasing the lock.  Do not lock batches here (the claimant already
    # owns the batch first), otherwise the opposite lock order can deadlock.
    task_ids = sorted(
        {
            int(allocation.pick_task_id)
            for allocation in allocations
            if allocation.pick_task_id
        }
    )
    tasks_by_id = {
        int(task.id): task
        for task in FbsPickTask.objects.select_related("batch")
        .filter(id__in=task_ids)
        .order_by("id")
    }

    blockers: list[str] = []
    queued_batch_ids: set[int] = set()
    reserved_by_balance = {
        int(row["balance_id"]): int(row["qty"] or 0)
        for row in FbsOrderStockAllocation.objects.filter(
            balance_id__in=normalized_balance_ids,
            status__in=ACTIVE_ALLOCATION_STATUSES,
        ).values("balance_id")
        .annotate(qty=Sum("qty_reserved"))
        .order_by()
    }
    balances = FbsStockBalance.objects.filter(id__in=normalized_balance_ids).order_by("id")
    for balance in balances:
        reserved_qty = int(balance.reserved_qty or 0)
        accounted_qty = int(reserved_by_balance.get(int(balance.id), 0) or 0)
        if reserved_qty > accounted_qty:
            blockers.append(
                "На остатке есть резерв без активной FBS-заявки; требуется проверка резерва."
            )

    for allocation in allocations:
        if (
            allocation.status == FbsOrderStockAllocation.STATUS_PICKING
            or int(allocation.qty_picked or 0) > 0
        ):
            blockers.append("Товар уже взят в работу подборщиком FBS.")
            continue
        task = tasks_by_id.get(int(allocation.pick_task_id or 0))
        if task is None:
            continue
        batch = task.batch
        if (
            task.status != FbsPickTask.STATUS_QUEUED
            or task.assigned_to_id is not None
            or task.claimed_at is not None
            or task.completed_at is not None
            or task.canceled_at is not None
            or int(task.picked_qty or 0) > 0
            or not _batch_is_untouched(batch)
        ):
            blockers.append("Волна FBS уже взята в работу; перемещение запрещено.")
            continue
        queued_batch_ids.add(int(batch.id))

    return RelocationReservationState(
        blockers=tuple(dict.fromkeys(blockers)),
        queued_batch_ids=tuple(sorted(queued_batch_ids)),
    )


def active_free_relocation_for_pick_tasks(
    task_ids,
    *,
    lock_allocations: bool = False,
) -> bool:
    """Protect claiming a wave while one of its source boxes is in transit."""

    normalized_task_ids = sorted({int(value) for value in task_ids if value})
    if not normalized_task_ids:
        return False
    allocations = FbsOrderStockAllocation.objects.filter(
        pick_task_id__in=normalized_task_ids,
        status__in=ACTIVE_ALLOCATION_STATUSES,
    ).order_by("id")
    if lock_allocations:
        allocations = allocations.select_for_update(of=("self",))
    source_ids = list(
        allocations.values_list("balance__box_id", "balance__box__pallet_id")
    )
    box_ids = sorted({int(box_id) for box_id, _pallet_id in source_ids if box_id})
    pallet_ids = sorted(
        {int(pallet_id) for _box_id, pallet_id in source_ids if pallet_id}
    )
    pallet_context_ids = [str(value) for value in pallet_ids]
    box_context_ids = [str(value) for value in box_ids]
    pallet_move = WarehouseOperation.objects.filter(
        context_type=FBS_FREE_PALLET_CONTEXT,
        context_id__in=pallet_context_ids,
        status=WarehouseOperation.STATUS_IN_PROGRESS,
    )
    box_move = WarehouseOperation.objects.filter(
        context_type=FBS_FREE_BOX_CONTEXT,
        context_id__in=box_context_ids,
        status=WarehouseOperation.STATUS_IN_PROGRESS,
    )
    return pallet_move.exists() or box_move.exists()
