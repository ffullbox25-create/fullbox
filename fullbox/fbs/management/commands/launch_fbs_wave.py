"""Автоматический запуск волн отбора FBS по расписанию.

Все готовые к подбору группы запускаются каждые полчаса с 08:00 до 19:30. Очередь
по умолчанию не имеет общего лимита на незапущенные волны. Автоматическая волна содержит не более
50 единиц, а для ООО «КЕЙЗИ» — не более 100. Индивидуальная настройка
кабинета имеет приоритет; клиенты и кабинеты в волнах не смешиваются.
Свободные места распределяются по кругу: не более одной новой волны на
кабинет за проход, причем дольше всех ожидавшие кабинеты идут первыми.
"""
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Exists, Max, OuterRef
from django.db.utils import OperationalError
from django.utils import timezone

from fbs.exceptions import FbsError
from fbs.models import (
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderStockAllocation,
    FbsPickBatch,
    FbsPickTask,
)
from fbs.services.picking import (
    ACTIVE_TASK_STATUSES,
    RESERVABLE_ORDER_STATUSES,
    _is_deadlock_error,
    _marketplace_queue_error,
    create_pick_batches,
    order_by_pick_priority,
    pick_order_priority_key,
    prepare_pick_queue,
    regroup_queued_pick_batches,
)
from fbs.services.wave_policy import (
    FORCED_AGENCY_WAVE_LIMITS,
    LEGACY_AGENCY_WAVE_LIMITS,
    configured_or_default_wave_limits,
)

WINDOW_START_MINUTES = 8 * 60         # 08:00
WINDOW_END_MINUTES = 19 * 60 + 30     # 19:30
DEFAULT_MAX_QUEUED_BATCHES = 0
DEFAULT_LIMIT = 200
DEFAULT_MAX_ORDERS_PER_BATCH = 50
AUTO_MAX_UNITS_PER_BATCH = 50
KEYZI_AGENCY_ID = 2951
KEYZI_MAX_ORDERS_PER_BATCH = 100
KEYZI_MAX_UNITS_PER_BATCH = 100
SLOT_MINUTES = (0, 30)
# systemd будит юнит с точностью до минуты, а старт Django занимает еще секунды.
# Без допуска запуск в 09:01:07 молча уходил бы в пропуск по расписанию.
SLOT_TOLERANCE_SECONDS = 120
# Волна одного клиента может не создаться из-за взаимной блокировки в базе:
# 06.09 в 14:30 из-за нее был потерян весь тик.
CLIENT_DEADLOCK_RETRY_ATTEMPTS = 3
CLIENT_DEADLOCK_RETRY_DELAY_SECONDS = 0.4

TICK_OPENING = "opening"
TICK_REGULAR = "regular"


@dataclass(frozen=True)
class ReadyAgencyWave:
    agency_id: int
    order_ids: tuple[int, ...]
    unit_count: int
    oldest_key: tuple[object, ...]
    profile_id: int | None = None
    max_orders_per_batch: int = DEFAULT_MAX_ORDERS_PER_BATCH
    max_units_per_batch: int = AUTO_MAX_UNITS_PER_BATCH


def _slot_kind(hour: int, minute: int):
    """Вид тика для точки сетки расписания."""
    slot_minutes = hour * 60 + minute
    if minute not in SLOT_MINUTES:
        return None
    if not WINDOW_START_MINUTES <= slot_minutes <= WINDOW_END_MINUTES:
        return None
    if slot_minutes == WINDOW_START_MINUTES:
        return TICK_OPENING
    return TICK_REGULAR


def _scheduled_slot(now, *, tolerance_seconds: int = SLOT_TOLERANCE_SECONDS):
    """Точка сетки, к которой относится запуск, если он не опоздал.

    Допуск односторонний: раньше срока systemd юнит не будит, поэтому раннее
    время слотом не считается, а опоздание в пределах допуска — считается.
    """
    best = None
    for hour_offset in (-1, 0):
        base = (now + timedelta(hours=hour_offset)).replace(
            minute=0, second=0, microsecond=0
        )
        for minute in SLOT_MINUTES:
            slot = base.replace(minute=minute)
            if _slot_kind(slot.hour, slot.minute) is None:
                continue
            delay = (now - slot).total_seconds()
            if 0 <= delay <= tolerance_seconds and (best is None or delay < best[0]):
                best = (delay, slot)
    return None if best is None else best[1]


def _scheduled_tick_kind(now):
    """Вид тика для локального московского времени с допуском на опоздание."""
    slot = _scheduled_slot(now)
    if slot is None:
        return None
    return _slot_kind(slot.hour, slot.minute)


def _groups_for_tick(groups, tick_kind):
    if tick_kind in {TICK_OPENING, TICK_REGULAR}:
        return tuple(group for group in groups if group.unit_count > 0)
    return ()


def _has_queued_capacity(queued_count: int, max_queued: int) -> bool:
    """Можно ли создать еще одну волну; ноль отключает общий лимит."""
    return max_queued == 0 or queued_count < max_queued


def _groups_in_fair_order(groups, *, latest_wave_at_by_profile=None):
    """Put never-served and longest-waiting FBS cabinets first."""
    groups = tuple(groups)
    if latest_wave_at_by_profile is None:
        profile_ids = tuple(
            group.profile_id
            for group in groups
            if group.profile_id is not None
        )
        latest_wave_at_by_profile = dict(
            FbsPickTask.objects.filter(order__profile_id__in=profile_ids)
            .exclude(batch__status=FbsPickBatch.STATUS_CANCELED)
            .exclude(status=FbsPickTask.STATUS_CANCELED)
            .order_by()
            .values("order__profile_id")
            .annotate(last_wave_at=Max("batch__created_at"))
            .values_list("order__profile_id", "last_wave_at")
        )
    return tuple(
        sorted(
            groups,
            key=lambda group: (
                group.profile_id in latest_wave_at_by_profile,
                latest_wave_at_by_profile.get(group.profile_id)
                or group.oldest_key[0],
                group.oldest_key,
                group.agency_id,
                group.profile_id or 0,
            ),
        )
    )


def _orders_ready_for_wave():
    """Заказы с резервом, но без активного задания отбора."""
    return FbsOrder.objects.filter(
        internal_status=FbsOrder.STATUS_RESERVED
    ).annotate(
        _has_task=Exists(
            FbsPickTask.objects.filter(
                order_id=OuterRef("pk"),
                status__in=ACTIVE_TASK_STATUSES,
            )
        )
    ).filter(_has_task=False)


def _auto_batch_limits_for_agency(
    agency_id: int,
    *,
    default_max_orders: int = DEFAULT_MAX_ORDERS_PER_BATCH,
) -> tuple[int, int]:
    agency_id = int(agency_id)
    if agency_id in FORCED_AGENCY_WAVE_LIMITS:
        return FORCED_AGENCY_WAVE_LIMITS[agency_id]
    if agency_id == KEYZI_AGENCY_ID:
        return KEYZI_MAX_ORDERS_PER_BATCH, KEYZI_MAX_UNITS_PER_BATCH
    return default_max_orders, AUTO_MAX_UNITS_PER_BATCH


def _auto_batch_limits_for_profile(
    profile,
    *,
    default_max_orders: int = DEFAULT_MAX_ORDERS_PER_BATCH,
) -> tuple[int, int]:
    limits = configured_or_default_wave_limits(profile)
    if (
        not limits.configured
        and int(profile.agency_id) not in LEGACY_AGENCY_WAVE_LIMITS
        and int(profile.agency_id) not in FORCED_AGENCY_WAVE_LIMITS
    ):
        return default_max_orders, AUTO_MAX_UNITS_PER_BATCH
    return limits.max_orders, int(limits.max_units or AUTO_MAX_UNITS_PER_BATCH)


def _auto_order_is_launchable(unit_count: int, *, agency_id: int | None = None) -> bool:
    max_units = (
        _auto_batch_limits_for_agency(agency_id)[1]
        if agency_id is not None
        else AUTO_MAX_UNITS_PER_BATCH
    )
    return 0 < int(unit_count or 0) <= max_units


def _ready_agency_waves():
    """Return actually launchable reserved units grouped by client."""
    orders = list(
        order_by_pick_priority(
            _orders_ready_for_wave().select_related(
                "profile__agency", "profile__wave_policy"
            )
        )
    )
    if not orders:
        return ()

    allocations_by_order = defaultdict(list)
    for allocation in (
        FbsOrderStockAllocation.objects.select_related(
            "order_item",
            "balance__box__pallet__cell__location"
        )
        .filter(
            order_item__order_id__in=[order.id for order in orders],
            status=FbsOrderStockAllocation.STATUS_RESERVED,
            pick_task__isnull=True,
        )
        .order_by("order_item__order_id", "id")
    ):
        allocations_by_order[allocation.order_item.order_id].append(allocation)

    grouped = {}
    for order in orders:
        allocations = allocations_by_order.get(order.id, ())
        if not allocations:
            continue
        if _marketplace_queue_error(order):
            continue
        profile = order.profile
        agency_id = profile.agency_id
        profile_max_orders, profile_max_units = _auto_batch_limits_for_profile(profile)
        unit_count = sum(int(allocation.qty_reserved or 0) for allocation in allocations)
        # An order is indivisible. If it alone exceeds the cabinet maximum,
        # create_pick_batches places it in an isolated wave instead of leaving
        # the order permanently outside the automatic queue.
        if unit_count <= 0:
            continue
        oldest_key = pick_order_priority_key(order)
        row = grouped.setdefault(
            profile.id,
            {
                "agency_id": agency_id,
                "profile_id": profile.id,
                "order_ids": [],
                "unit_count": 0,
                "oldest_key": oldest_key,
                "max_orders_per_batch": profile_max_orders,
                "max_units_per_batch": profile_max_units,
            },
        )
        row["order_ids"].append(order.id)
        row["unit_count"] += unit_count
        if oldest_key < row["oldest_key"]:
            row["oldest_key"] = oldest_key

    return tuple(
        ReadyAgencyWave(
            agency_id=row["agency_id"],
            profile_id=row["profile_id"],
            order_ids=tuple(row["order_ids"]),
            unit_count=row["unit_count"],
            oldest_key=row["oldest_key"],
            max_orders_per_batch=row["max_orders_per_batch"],
            max_units_per_batch=row["max_units_per_batch"],
        )
        for row in sorted(
            grouped.values(),
            key=lambda value: (
                value["oldest_key"],
                value["agency_id"],
                value["profile_id"],
            ),
        )
    )


def _create_client_batches(
    group,
    *,
    max_orders_per_batch: int,
    max_units_per_batch: int,
    max_new_batches: int,
):
    """Создает волны одного клиента, переигрывая взаимную блокировку в базе."""
    for attempt in range(CLIENT_DEADLOCK_RETRY_ATTEMPTS):
        try:
            return create_pick_batches(
                order_ids=group.order_ids,
                max_orders_per_batch=max_orders_per_batch,
                max_units_per_batch=max_units_per_batch,
                reuse_queued_batches=True,
                max_new_batches=max_new_batches,
            )
        except OperationalError as exc:
            if (
                not _is_deadlock_error(exc)
                or attempt + 1 >= CLIENT_DEADLOCK_RETRY_ATTEMPTS
            ):
                raise
            time.sleep(CLIENT_DEADLOCK_RETRY_DELAY_SECONDS * (attempt + 1))
    return ()


class Command(BaseCommand):
    help = "Создает волны FBS каждые полчаса с 08:00 до 19:30."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Создать волну. Без флага только показывает, что было бы сделано.",
        )
        parser.add_argument(
            "--max-queued",
            type=int,
            default=DEFAULT_MAX_QUEUED_BATCHES,
            help=(
                "Сколько незапущенных волн допустимо. "
                "Ноль означает отсутствие общего лимита."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_LIMIT,
            help="Сколько новых заказов осматривать на резервирование.",
        )
        parser.add_argument(
            "--max-orders",
            type=int,
            default=DEFAULT_MAX_ORDERS_PER_BATCH,
            help="Размер волны в заказах: допустимо только 50 или 100.",
        )
        parser.add_argument(
            "--ignore-window",
            action="store_true",
            help="Игнорировать расписание и обработать все размеры волн вручную.",
        )

    def handle(self, *args, **options):
        now = timezone.localtime()
        slot = _scheduled_slot(now)
        slot_label = f"{slot:%H:%M}" if slot is not None else "-"
        max_queued = int(options["max_queued"])
        limit = int(options["limit"])
        max_orders = int(options["max_orders"])

        if max_orders not in {50, 100}:
            raise CommandError("Размер волны может быть только 50 или 100 заказов.")
        if max_queued < 0:
            raise CommandError("Лимит незапущенных волн не может быть отрицательным.")
        if limit <= 0:
            raise CommandError("Лимит осмотра заказов должен быть больше нуля.")

        tick_kind = (
            TICK_OPENING
            if options["ignore_window"]
            else _scheduled_tick_kind(now)
        )
        if tick_kind is None:
            self.stdout.write(
                "FBS_WAVE_SKIP reason=schedule "
                f"now={now:%H:%M:%S} window=08:00-19:30"
            )
            return

        if options["apply"]:
            queued_profile_ids = tuple(
                FbsPickTask.objects.filter(
                    batch__status=FbsPickBatch.STATUS_QUEUED,
                    status=FbsPickTask.STATUS_QUEUED,
                )
                .values_list("order__profile_id", flat=True)
                .distinct()
            )
            profiles = FbsIntegrationProfile.objects.select_related(
                "agency", "wave_policy"
            ).filter(pk__in=queued_profile_ids).order_by("id")
            for profile in profiles:
                profile_max_orders, profile_max_units = _auto_batch_limits_for_profile(
                    profile,
                    default_max_orders=max_orders,
                )
                try:
                    regrouped = regroup_queued_pick_batches(
                        profile_id=profile.id,
                        max_orders_per_batch=profile_max_orders,
                        max_units_per_batch=profile_max_units,
                    )
                except (FbsError, OperationalError) as exc:
                    reason = str(exc).replace("\n", " ")[:300]
                    self.stdout.write(
                        self.style.WARNING(
                            f"FBS_WAVE_REGROUP_FAILED profile={profile.id} reason={reason}"
                        )
                    )
                else:
                    self.stdout.write(
                        "FBS_WAVE_REGROUP "
                        f"profile={profile.id} batches={regrouped.batch_count} "
                        f"moved_tasks={regrouped.moved_task_count} "
                        f"emptied={regrouped.emptied_batch_count}"
                    )

        queued = FbsPickBatch.objects.filter(
            status=FbsPickBatch.STATUS_QUEUED
        ).count()

        new_candidates = FbsOrder.objects.filter(
            internal_status__in=RESERVABLE_ORDER_STATUSES,
            profile__is_active=True,
        ).count()
        ready_before = _orders_ready_for_wave().count()

        if not new_candidates and not ready_before:
            self.stdout.write("FBS_WAVE_SKIP reason=no_orders")
            return

        if not options["apply"]:
            ready_groups = _ready_agency_waves()
            selected_groups = _groups_for_tick(ready_groups, tick_kind)
            self.stdout.write(
                f"FBS_WAVE_DRY_RUN now={now:%H:%M} tick={tick_kind} "
                f"queued={queued} new_candidates={new_candidates} "
                f"ready_for_wave={ready_before} ready_units="
                f"{sum(group.unit_count for group in ready_groups)} "
                f"selected_clients={len(selected_groups)} selected_units="
                f"{sum(group.unit_count for group in selected_groups)} "
                f"limit={limit} max_orders={max_orders} "
                f"max_units={AUTO_MAX_UNITS_PER_BATCH} "
                f"keyzi_max_units={KEYZI_MAX_UNITS_PER_BATCH} "
                f"max_queued={max_queued}"
            )
            return

        reserved = awaiting = failed = inspected = 0
        rejected = ()
        if new_candidates:
            try:
                result = prepare_pick_queue(
                    limit=limit,
                    max_orders_per_batch=max_orders,
                    max_units_per_batch=AUTO_MAX_UNITS_PER_BATCH,
                    create_batches=False,
                )
            except FbsError as exc:
                self.stdout.write(f"FBS_WAVE_RESERVE_FAILED {exc}")
            else:
                inspected = result.inspected_orders
                reserved = result.reserved_orders
                awaiting = result.awaiting_stock_orders
                failed = result.validation_failed_orders
                rejected = result.rejected_orders

        ready_groups = _ready_agency_waves()
        selected_groups = _groups_for_tick(ready_groups, tick_kind)
        selected_order_ids = tuple(
            order_id
            for group in selected_groups
            for order_id in group.order_ids
        )
        selected_units = sum(group.unit_count for group in selected_groups)
        deferred_groups = tuple(
            group for group in ready_groups if group not in selected_groups
        )
        deferred_units = sum(group.unit_count for group in deferred_groups)

        if not selected_order_ids:
            self.stdout.write(
                "FBS_WAVE_SKIP reason=unit_threshold "
                f"now={now:%H:%M} tick={tick_kind} "
                f"ready_clients={len(ready_groups)} ready_units="
                f"{sum(group.unit_count for group in ready_groups)} "
                f"deferred_clients={len(deferred_groups)} "
                f"deferred_units={deferred_units} "
                f"inspected={inspected} reserved={reserved} "
                f"awaiting_stock={awaiting} validation_failed={failed}"
            )
            return

        batches_by_id = {}
        client_failures = []
        pending_groups = list(_groups_in_fair_order(selected_groups))
        while pending_groups:
            next_round = []
            created_in_round = False
            for group in pending_groups:
                if group.profile_id is None:
                    group_max_orders, group_max_units = _auto_batch_limits_for_agency(
                        group.agency_id,
                        default_max_orders=max_orders,
                    )
                else:
                    group_max_orders = group.max_orders_per_batch
                    group_max_units = group.max_units_per_batch
                queued_batch_ids = set(
                    FbsPickBatch.objects.filter(
                        status=FbsPickBatch.STATUS_QUEUED
                    ).values_list("id", flat=True)
                )
                max_new_for_group = (
                    1
                    if _has_queued_capacity(len(queued_batch_ids), max_queued)
                    else 0
                )
                try:
                    group_batches = _create_client_batches(
                        group,
                        max_orders_per_batch=group_max_orders,
                        max_units_per_batch=group_max_units,
                        max_new_batches=max_new_for_group,
                    )
                except (FbsError, OperationalError) as exc:
                    # Сбой по одному клиенту не должен стоить всего тика: волны
                    # остальных клиентов создаются, причина уходит в журнал.
                    client_failures.append((group.agency_id, str(exc).replace("\n", " ")[:300]))
                    self.stdout.write(
                        self.style.WARNING(
                            f"FBS_WAVE_FAILED agency={group.agency_id} "
                            f"orders={len(group.order_ids)} reason={client_failures[-1][1]}"
                        )
                    )
                    continue
                created_new_batch = any(
                    batch.id not in queued_batch_ids
                    for batch in group_batches
                )
                if created_new_batch:
                    created_in_round = True
                    next_round.append(group)
                for batch in group_batches:
                    batches_by_id[batch.id] = batch
            if not created_in_round:
                break
            if not _has_queued_capacity(
                FbsPickBatch.objects.filter(
                    status=FbsPickBatch.STATUS_QUEUED
                ).count(),
                max_queued,
            ):
                break
            pending_groups = next_round
        batches = tuple(batches_by_id.values())

        ready_after = _orders_ready_for_wave().count()
        if not batches:
            reason = (
                "all_clients_failed"
                if client_failures and len(client_failures) == len(selected_groups)
                else "queue_full_no_reusable_capacity"
            )
            self.stdout.write(
                f"FBS_WAVE_SKIP reason={reason} "
                f"queued={queued} limit={max_queued} tick={tick_kind} "
                f"failed_clients={len(client_failures)} "
                f"ready_after={ready_after}"
            )
            if reason == "all_clients_failed":
                # Ни одной волны и сбой по каждому выбранному клиенту — это
                # авария тика, а не спокойный пропуск: пусть юнит упадет.
                raise CommandError(
                    "Волны не созданы: сбой по всем выбранным клиентам "
                    f"({len(client_failures)})."
                )
            return

        batch_ids = ",".join(str(batch.id) for batch in batches) or "-"
        self.stdout.write(
            self.style.SUCCESS(
                "FBS_WAVE_LAUNCH "
                f"now={now:%H:%M:%S} slot={slot_label} tick={tick_kind} "
                f"batches={len(batches)} ids={batch_ids} "
                f"selected_clients={len(selected_groups)} "
                f"selected_units={selected_units} "
                f"deferred_clients={len(deferred_groups)} "
                f"deferred_units={deferred_units} "
                f"ready_before={ready_before} inspected={inspected} "
                f"reserved={reserved} awaiting_stock={awaiting} "
                f"validation_failed={failed} rejected={len(rejected)} "
                f"failed_clients={len(client_failures)} "
                f"ready_after={ready_after} max_units={AUTO_MAX_UNITS_PER_BATCH} "
                f"keyzi_max_units={KEYZI_MAX_UNITS_PER_BATCH} "
                f"max_queued={max_queued} "
                "all_tiers_enabled=1"
            )
        )
        for agency_id, reason in client_failures:
            self.stdout.write(f"FBS_WAVE_FAILED agency={agency_id} reason={reason}")
        for rejection in rejected[:20]:
            self.stdout.write(f"FBS_WAVE_REJECTED {rejection}")
