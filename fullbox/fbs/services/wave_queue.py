from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import (
    Case,
    Exists,
    IntegerField,
    OuterRef,
    QuerySet,
    Subquery,
    Value,
    When,
)
from django.utils import timezone

from fbs.exceptions import FbsPickingError
from fbs.models import (
    FbsIntegrationProfile,
    FbsMarketplaceQueuePriority,
    FbsPickBatch,
    FbsPickTask,
)


DEFAULT_MARKETPLACE_PRIORITY = 100
MIN_QUEUE_PRIORITY = 1
MAX_QUEUE_PRIORITY = 100
QUEUE_ACTIONS = {"top", "up", "down", "bottom"}


@dataclass(frozen=True)
class MarketplaceQueuePriorityRow:
    marketplace: str
    label: str
    priority: int
    effective_from: date
    is_active: bool
    configured: bool


def untouched_wave_queue_queryset(queryset: QuerySet | None = None) -> QuerySet:
    """Return waves that have never been claimed and are safe to reorder."""
    queryset = queryset if queryset is not None else FbsPickBatch.objects.all()
    queued_tasks = FbsPickTask.objects.filter(
        batch_id=OuterRef("pk"),
        status=FbsPickTask.STATUS_QUEUED,
    )
    started_tasks = FbsPickTask.objects.filter(batch_id=OuterRef("pk")).exclude(
        status__in=(FbsPickTask.STATUS_QUEUED, FbsPickTask.STATUS_CANCELED)
    )
    return (
        queryset.filter(
            status=FbsPickBatch.STATUS_QUEUED,
            assigned_to__isnull=True,
            workstation__isnull=True,
            cart__isnull=True,
            started_at__isnull=True,
            claimed_at__isnull=True,
            picking_completed_at__isnull=True,
            verification_assigned_to__isnull=True,
            verification_started_at__isnull=True,
            cart_released_at__isnull=True,
            completed_at__isnull=True,
            canceled_at__isnull=True,
            picked_qty=0,
        )
        .annotate(
            _queue_has_task=Exists(queued_tasks),
            _queue_has_started_task=Exists(started_tasks),
        )
        .filter(_queue_has_task=True, _queue_has_started_task=False)
    )


def _active_marketplace_priorities(on_date: date) -> dict[str, int]:
    return {
        str(marketplace): int(priority)
        for marketplace, priority in FbsMarketplaceQueuePriority.objects.filter(
            is_active=True,
            effective_from__lte=on_date,
        ).values_list("marketplace", "priority")
    }


def order_wave_queue_queryset(
    queryset: QuerySet | None = None,
    *,
    on_date: date | None = None,
) -> QuerySet:
    """Apply the single canonical free-wave order used by UI and picker claims."""
    on_date = on_date or timezone.localdate()
    queryset = untouched_wave_queue_queryset(queryset)
    marketplace = Subquery(
        FbsPickTask.objects.filter(
            batch_id=OuterRef("pk"),
            status=FbsPickTask.STATUS_QUEUED,
        )
        .order_by("sort_order", "id")
        .values("order__profile__marketplace")[:1]
    )
    priorities = _active_marketplace_priorities(on_date)
    marketplace_whens = [
        When(_queue_marketplace=key, then=Value(priority))
        for key, priority in sorted(priorities.items(), key=lambda item: item[1])
    ]
    return (
        queryset.annotate(
            _queue_marketplace=marketplace,
            _queue_manual_group=Case(
                When(
                    manual_queue_date=on_date,
                    manual_queue_rank__isnull=False,
                    then=Value(0),
                ),
                default=Value(1),
                output_field=IntegerField(),
            ),
            _queue_manual_rank=Case(
                When(
                    manual_queue_date=on_date,
                    manual_queue_rank__isnull=False,
                    then="manual_queue_rank",
                ),
                default=Value(0),
                output_field=IntegerField(),
            ),
            _queue_marketplace_rank=Case(
                *marketplace_whens,
                default=Value(DEFAULT_MARKETPLACE_PRIORITY),
                output_field=IntegerField(),
            ),
        )
        .order_by(
            "_queue_manual_group",
            "_queue_manual_rank",
            "_queue_marketplace_rank",
            "created_at",
            "id",
        )
    )


def decorate_wave_queue_rows(rows, *, on_date: date | None = None) -> None:
    on_date = on_date or timezone.localdate()
    labels = dict(FbsIntegrationProfile.MARKETPLACE_CHOICES)
    priorities = _active_marketplace_priorities(on_date)
    for position, batch in enumerate(rows, start=1):
        marketplace = str(getattr(batch, "_queue_marketplace", "") or "")
        batch.queue_position = position
        batch.queue_marketplace = marketplace
        batch.queue_marketplace_label = labels.get(marketplace, "Не определён")
        if batch.manual_queue_date == on_date and batch.manual_queue_rank is not None:
            batch.queue_priority_label = "Ручной порядок на сегодня"
        elif marketplace in priorities:
            batch.queue_priority_label = (
                f"{batch.queue_marketplace_label}: приоритет {priorities[marketplace]}"
            )
        else:
            batch.queue_priority_label = "По времени создания"


def current_wave_queue(*, limit: int = 500) -> list[FbsPickBatch]:
    rows = list(
        order_wave_queue_queryset(
            FbsPickBatch.objects.select_related("agency", "queue_order_updated_by")
        )[: max(int(limit or 0), 1)]
    )
    decorate_wave_queue_rows(rows)
    return rows


def _persist_manual_queue_order(*, queue: list[FbsPickBatch], actor) -> tuple[int, ...]:
    today = timezone.localdate()
    now = timezone.now()
    for rank, batch in enumerate(queue, start=1):
        batch.manual_queue_rank = rank
        batch.manual_queue_date = today
        batch.queue_order_updated_by = actor
        batch.updated_at = now
    if queue:
        FbsPickBatch.objects.bulk_update(
            queue,
            [
                "manual_queue_rank",
                "manual_queue_date",
                "queue_order_updated_by",
                "updated_at",
            ],
        )
    return tuple(batch.id for batch in queue)


@transaction.atomic
def reorder_queued_wave(*, batch_id: int, action: str, actor) -> tuple[int, ...]:
    """Move one untouched wave and persist the complete queue order for today."""
    action = str(action or "").strip().lower()
    if action not in QUEUE_ACTIONS:
        raise FbsPickingError("Неизвестное действие с очередью волн.")
    queue = list(
        order_wave_queue_queryset(
            FbsPickBatch.objects.select_for_update().select_related("agency")
        )
    )
    target_index = next(
        (index for index, batch in enumerate(queue) if batch.id == int(batch_id)),
        None,
    )
    if target_index is None:
        raise FbsPickingError(
            "Волна уже взята в работу или изменилась. Обновите очередь."
        )
    target = queue.pop(target_index)
    if action == "top":
        new_index = 0
    elif action == "bottom":
        new_index = len(queue)
    elif action == "up":
        new_index = max(target_index - 1, 0)
    else:
        new_index = min(target_index + 1, len(queue))
    queue.insert(new_index, target)
    return _persist_manual_queue_order(queue=queue, actor=actor)


@transaction.atomic
def set_manual_wave_order_for_today(
    *, batch_ids: list[int] | tuple[int, ...], actor
) -> tuple[int, ...]:
    """Persist one complete, race-safe order for the current untouched queue."""
    queue = list(
        order_wave_queue_queryset(
            FbsPickBatch.objects.select_for_update().select_related("agency")
        )
    )
    normalized_ids = tuple(int(batch_id) for batch_id in batch_ids)
    if len(normalized_ids) != len(set(normalized_ids)):
        raise FbsPickingError("В переданном порядке есть повторяющиеся волны.")
    batch_by_id = {batch.id: batch for batch in queue}
    if set(normalized_ids) != set(batch_by_id):
        raise FbsPickingError(
            "Очередь волн изменилась. Обновите список и повторите настройку."
        )
    ordered_queue = [batch_by_id[batch_id] for batch_id in normalized_ids]
    return _persist_manual_queue_order(queue=ordered_queue, actor=actor)


@transaction.atomic
def keep_current_wave_order_for_today(*, actor) -> tuple[int, ...]:
    """Freeze the currently visible free-wave order until the local day ends."""
    queue = list(
        order_wave_queue_queryset(FbsPickBatch.objects.select_for_update())
    )
    return _persist_manual_queue_order(queue=queue, actor=actor)


@transaction.atomic
def clear_today_manual_wave_order() -> int:
    return FbsPickBatch.objects.filter(
        manual_queue_date=timezone.localdate(),
        status=FbsPickBatch.STATUS_QUEUED,
        assigned_to__isnull=True,
    ).update(manual_queue_rank=None, manual_queue_date=None)


@transaction.atomic
def update_marketplace_queue_priorities(
    *,
    priorities: dict[str, int],
    effective_from: date,
    actor,
) -> tuple[FbsMarketplaceQueuePriority, ...]:
    known = dict(FbsIntegrationProfile.MARKETPLACE_CHOICES)
    normalized: dict[str, int] = {}
    for marketplace in known:
        try:
            priority = int(priorities[marketplace])
        except (KeyError, TypeError, ValueError):
            raise ValidationError(
                f"Укажите приоритет для маркетплейса {known[marketplace]}."
            )
        if not MIN_QUEUE_PRIORITY <= priority <= MAX_QUEUE_PRIORITY:
            raise ValidationError("Приоритет маркетплейса должен быть от 1 до 100.")
        normalized[marketplace] = priority
    if len(set(normalized.values())) != len(normalized):
        raise ValidationError("Приоритеты маркетплейсов не должны совпадать.")
    if not isinstance(effective_from, date):
        raise ValidationError("Укажите дату начала действия приоритета.")

    existing = {
        row.marketplace: row
        for row in FbsMarketplaceQueuePriority.objects.select_for_update().filter(
            marketplace__in=known
        )
    }
    saved = []
    for marketplace, priority in normalized.items():
        row = existing.get(marketplace) or FbsMarketplaceQueuePriority(
            marketplace=marketplace
        )
        row.priority = priority
        row.effective_from = effective_from
        row.is_active = True
        row.updated_by = actor
        row.full_clean(exclude={"id"})
        row.save()
        saved.append(row)
    return tuple(saved)


def marketplace_queue_priority_rows(
    *, on_date: date | None = None
) -> tuple[MarketplaceQueuePriorityRow, ...]:
    on_date = on_date or timezone.localdate()
    configured = {
        row.marketplace: row
        for row in FbsMarketplaceQueuePriority.objects.select_related("updated_by")
    }
    rows = []
    for fallback_priority, (marketplace, label) in enumerate(
        FbsIntegrationProfile.MARKETPLACE_CHOICES,
        start=1,
    ):
        row = configured.get(marketplace)
        rows.append(
            MarketplaceQueuePriorityRow(
                marketplace=marketplace,
                label=label,
                priority=(
                    int(row.priority)
                    if row is not None
                    else fallback_priority * 10
                ),
                effective_from=(row.effective_from if row is not None else on_date),
                is_active=(bool(row.is_active) if row is not None else False),
                configured=row is not None,
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.priority, row.marketplace)))
