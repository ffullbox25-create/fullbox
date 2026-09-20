from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from django.db import transaction
from django.db.models import (
    BooleanField,
    Case,
    CharField,
    Exists,
    OuterRef,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.functions import Cast
from django.utils import timezone

from fbs.barcode_aliases import normalize_barcode, same_sku_barcode_variant
from fbs.exceptions import FbsFeatureDisabled, FbsInventoryError
from fbs.flags import feature_enabled
from fbs.models import (
    FbsInternalMovement,
    FbsInventoryLine,
    FbsInventoryScan,
    FbsInventorySession,
    FbsInventoryWorkEvent,
    FbsOrderStockAllocation,
    FbsPickTask,
    FbsRackStagingBox,
    FbsReplenishmentAllocation,
    FbsStockBalance,
    FbsStorageLock,
)
from marking.codes import (
    MarkingCodeFormatError,
    marking_code_identity,
    marking_code_variants,
    normalize_marking_code,
    validate_import_marking_code,
)
from sklad.models import WarehouseEvent, WarehouseOperation
from sku.models import SKUBarcode


FBS_FREE_RELOCATION_CONTEXT = "fbs_free_relocation"


@dataclass(frozen=True)
class _ValidatedBoxLockBatch:
    box_ids: frozenset[int]
    for_execution: bool
    connection_alias: str


def _require_writes() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Складские операции FBS выключены.")


def _authenticated_user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _lock_scope_query() -> Q:
    return (
        Q(scope_type=FbsInventorySession.SCOPE_ALL)
        | Q(
            scope_type=FbsInventorySession.SCOPE_AGENCY,
            agency_id=OuterRef("agency_id"),
        )
        | Q(
            scope_type=FbsInventorySession.SCOPE_CELL,
            cell_id=OuterRef("box__pallet__cell_id"),
        )
        | Q(
            scope_type=FbsInventorySession.SCOPE_PALLET,
            pallet_id=OuterRef("box__pallet_id"),
        )
        | Q(
            scope_type=FbsInventorySession.SCOPE_BOX,
            box_id=OuterRef("box_id"),
        )
        | Q(
            scope_type=FbsInventorySession.SCOPE_SKU,
            agency_id=OuterRef("agency_id"),
            sku_id=OuterRef("sku_ref_id"),
        )
    )


def with_fbs_lock_state(
    queryset: QuerySet,
    *,
    for_execution: bool = False,
    ignore_internal_movement: bool = False,
) -> QuerySet:
    flag = "block_execution" if for_execution else "block_new_reservations"
    locks = FbsStorageLock.objects.filter(is_active=True, **{flag: True}).filter(
        _lock_scope_query()
    )
    active_relocations = WarehouseOperation.objects.filter(
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type=FBS_FREE_RELOCATION_CONTEXT,
        context_id=Cast(OuterRef("box__pallet_id"), output_field=CharField()),
        agency_id=OuterRef("agency_id"),
        status=WarehouseOperation.STATUS_IN_PROGRESS,
    )
    active_rack_staging = FbsRackStagingBox.objects.filter(
        box_id=OuterRef("box_id"),
        status=FbsRackStagingBox.STATUS_AWAITING,
    )
    lock_conditions = [
        When(Exists(locks), then=Value(True)),
    ]
    if not ignore_internal_movement:
        lock_conditions.append(When(Exists(active_relocations), then=Value(True)))
    if not for_execution and not ignore_internal_movement:
        lock_conditions.append(When(Exists(active_rack_staging), then=Value(True)))
    return queryset.annotate(
        _fbs_is_locked=Case(
            *lock_conditions,
            default=Value(False),
            output_field=BooleanField(),
        )
    )


def cancel_inventory_locks_for_order_reservation(
    *,
    balance_ids,
    order_id: int,
    external_order_id: str = "",
    canceled_by=None,
) -> tuple[int, ...]:
    """Cancel active inventories that block stock required by an FBS order.

    The caller must pass only balances that are both relevant to the order and
    locked by a real ``FbsStorageLock``. Internal relocation and PR rack
    staging are intentionally outside this path: they no longer block order
    reservation and their operational state must remain unchanged.
    """
    _require_writes()
    normalized_balance_ids = {
        int(balance_id)
        for balance_id in balance_ids
        if int(balance_id or 0) > 0
    }
    if not normalized_balance_ids:
        return ()

    balances = list(
        FbsStockBalance.objects.filter(pk__in=normalized_balance_ids).values(
            "agency_id",
            "box_id",
            "box__pallet_id",
            "box__pallet__cell_id",
            "sku_ref_id",
        )
    )
    if not balances:
        return ()

    agency_ids = {row["agency_id"] for row in balances if row["agency_id"]}
    box_ids = {row["box_id"] for row in balances if row["box_id"]}
    pallet_ids = {
        row["box__pallet_id"] for row in balances if row["box__pallet_id"]
    }
    cell_ids = {
        row["box__pallet__cell_id"]
        for row in balances
        if row["box__pallet__cell_id"]
    }
    lock_scope = Q(scope_type=FbsInventorySession.SCOPE_ALL)
    if agency_ids:
        lock_scope |= Q(
            scope_type=FbsInventorySession.SCOPE_AGENCY,
            agency_id__in=agency_ids,
        )
    if cell_ids:
        lock_scope |= Q(
            scope_type=FbsInventorySession.SCOPE_CELL,
            cell_id__in=cell_ids,
        )
    if pallet_ids:
        lock_scope |= Q(
            scope_type=FbsInventorySession.SCOPE_PALLET,
            pallet_id__in=pallet_ids,
        )
    if box_ids:
        lock_scope |= Q(
            scope_type=FbsInventorySession.SCOPE_BOX,
            box_id__in=box_ids,
        )
    for agency_id, sku_id in {
        (row["agency_id"], row["sku_ref_id"])
        for row in balances
        if row["agency_id"] and row["sku_ref_id"]
    }:
        lock_scope |= Q(
            scope_type=FbsInventorySession.SCOPE_SKU,
            agency_id=agency_id,
            sku_id=sku_id,
        )

    session_ids = list(
        FbsStorageLock.objects.select_for_update()
        .filter(
            is_active=True,
            block_new_reservations=True,
        )
        .filter(lock_scope)
        .order_by("session_id")
        .values_list("session_id", flat=True)
    )
    if not session_ids:
        return ()

    now = timezone.now()
    actor = _authenticated_user(canceled_by)
    canceled_session_ids = []
    sessions = list(
        FbsInventorySession.objects.select_for_update()
        .filter(pk__in=session_ids)
        .order_by("id")
    )
    for session in sessions:
        FbsStorageLock.objects.filter(session=session, is_active=True).update(
            is_active=False,
            released_at=now,
            updated_at=now,
        )
        if session.status in {
            FbsInventorySession.STATUS_DONE,
            FbsInventorySession.STATUS_CANCELED,
        }:
            continue
        previous_status = session.status
        session.status = FbsInventorySession.STATUS_CANCELED
        session.completed_at = now
        session.save(update_fields=["status", "completed_at", "updated_at"])
        FbsInventoryWorkEvent.objects.create(
            session=session,
            action="canceled_for_order_reservation",
            actor=actor,
            payload={
                "order_id": int(order_id),
                "external_order_id": str(external_order_id or ""),
                "previous_status": previous_status,
                "reason": "FBS order requires stock locked by inventory",
            },
        )
        canceled_session_ids.append(session.id)
    return tuple(canceled_session_ids)


def filter_unlocked_balances(
    queryset: QuerySet,
    *,
    for_execution: bool = False,
) -> QuerySet:
    return with_fbs_lock_state(queryset, for_execution=for_execution).filter(
        _fbs_is_locked=False
    )


def balance_is_locked(balance_id: int, *, for_execution: bool = False) -> bool:
    return bool(
        with_fbs_lock_state(
            FbsStockBalance.objects.filter(pk=balance_id),
            for_execution=for_execution,
        ).values_list("_fbs_is_locked", flat=True).first()
    )


def pallet_is_in_free_relocation(pallet_id: int, *, agency_id: int | None = None) -> bool:
    queryset = WarehouseOperation.objects.filter(
        operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
        context_type=FBS_FREE_RELOCATION_CONTEXT,
        context_id=str(int(pallet_id)),
        status=WarehouseOperation.STATUS_IN_PROGRESS,
    )
    if agency_id:
        queryset = queryset.filter(agency_id=int(agency_id))
    return queryset.exists()


def assert_pallet_not_relocating(pallet_id: int, *, agency_id: int | None = None) -> None:
    if pallet_is_in_free_relocation(pallet_id, agency_id=agency_id):
        raise FbsInventoryError("FBS-паллета находится в свободном перемещении.")


def assert_balance_unlocked(balance_id: int, *, for_execution: bool = False) -> None:
    if balance_is_locked(balance_id, for_execution=for_execution):
        action = "выполнение операции" if for_execution else "новый резерв"
        raise FbsInventoryError(
            f"Инвентаризация или перемещение блокирует {action} по этому остатку."
        )


def box_is_locked(box_id: int, *, for_execution: bool = False) -> bool:
    from fbs.models import FbsBox

    box = FbsBox.objects.select_related("pallet__cell").get(pk=box_id)
    flag = "block_execution" if for_execution else "block_new_reservations"
    if pallet_is_in_free_relocation(box.pallet_id, agency_id=box.agency_id):
        return True
    if not for_execution and FbsRackStagingBox.objects.filter(
        box_id=box.id,
        status=FbsRackStagingBox.STATUS_AWAITING,
    ).exists():
        return True
    return FbsStorageLock.objects.filter(is_active=True, **{flag: True}).filter(
        Q(scope_type=FbsInventorySession.SCOPE_ALL)
        | Q(scope_type=FbsInventorySession.SCOPE_AGENCY, agency_id=box.agency_id)
        | Q(scope_type=FbsInventorySession.SCOPE_CELL, cell_id=box.pallet.cell_id)
        | Q(scope_type=FbsInventorySession.SCOPE_PALLET, pallet_id=box.pallet_id)
        | Q(scope_type=FbsInventorySession.SCOPE_BOX, box_id=box.id)
        | Q(
            scope_type=FbsInventorySession.SCOPE_SKU,
            agency_id=box.agency_id,
            sku_id__in=box.stock_balances.values("sku_ref_id"),
        )
    ).exists()


def validate_boxes_unlocked(
    box_ids,
    *,
    for_execution: bool = False,
) -> _ValidatedBoxLockBatch:
    """Lock and validate many boxes once inside the current DB transaction."""
    from fbs.models import FbsBox

    connection = transaction.get_connection()
    if not connection.in_atomic_block:
        raise FbsInventoryError(
            "Пакетная проверка FBS-коробов разрешена только внутри транзакции."
        )
    normalized_ids = frozenset(
        int(box_id) for box_id in box_ids if int(box_id or 0) > 0
    )
    if not normalized_ids:
        return _ValidatedBoxLockBatch(
            box_ids=normalized_ids,
            for_execution=bool(for_execution),
            connection_alias=connection.alias,
        )

    boxes = list(
        FbsBox.objects.select_for_update()
        .select_related("pallet__cell")
        .filter(pk__in=normalized_ids)
        .order_by("id")
    )
    loaded_ids = {int(box.id) for box in boxes}
    if loaded_ids != set(normalized_ids):
        raise FbsInventoryError("Часть FBS-коробов для проверки не найдена.")

    sku_ids_by_box: dict[int, set[int]] = {box_id: set() for box_id in loaded_ids}
    for box_id, sku_id in FbsStockBalance.objects.filter(
        box_id__in=normalized_ids,
        sku_ref_id__isnull=False,
    ).values_list("box_id", "sku_ref_id"):
        sku_ids_by_box[int(box_id)].add(int(sku_id))

    pallet_contexts = {str(int(box.pallet_id)) for box in boxes}
    relocating = {
        (str(context_id), int(agency_id))
        for context_id, agency_id in WarehouseOperation.objects.filter(
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            context_type=FBS_FREE_RELOCATION_CONTEXT,
            context_id__in=pallet_contexts,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        ).values_list("context_id", "agency_id")
        if agency_id
    }
    flag = "block_execution" if for_execution else "block_new_reservations"
    if not for_execution and FbsRackStagingBox.objects.filter(
        box_id__in=normalized_ids,
        status=FbsRackStagingBox.STATUS_AWAITING,
    ).exists():
        raise FbsInventoryError(
            "FBS-короб ожидает размещения в ячейку PR-стеллажа и недоступен для нового резерва."
        )
    locks = list(
        FbsStorageLock.objects.filter(is_active=True, **{flag: True}).values(
            "scope_type",
            "agency_id",
            "cell_id",
            "pallet_id",
            "box_id",
            "sku_id",
        )
    )
    for box in boxes:
        agency_id = int(box.agency_id)
        if (str(int(box.pallet_id)), agency_id) in relocating:
            raise FbsInventoryError(
                "Инвентаризация или перемещение блокирует операции с этим FBS-коробом."
            )
        box_sku_ids = sku_ids_by_box[int(box.id)]
        for lock in locks:
            scope_type = lock["scope_type"]
            is_locked = (
                scope_type == FbsInventorySession.SCOPE_ALL
                or (
                    scope_type == FbsInventorySession.SCOPE_AGENCY
                    and lock["agency_id"] == box.agency_id
                )
                or (
                    scope_type == FbsInventorySession.SCOPE_CELL
                    and lock["cell_id"] == box.pallet.cell_id
                )
                or (
                    scope_type == FbsInventorySession.SCOPE_PALLET
                    and lock["pallet_id"] == box.pallet_id
                )
                or (
                    scope_type == FbsInventorySession.SCOPE_BOX
                    and lock["box_id"] == box.id
                )
                or (
                    scope_type == FbsInventorySession.SCOPE_SKU
                    and lock["agency_id"] == box.agency_id
                    and lock["sku_id"] in box_sku_ids
                )
            )
            if is_locked:
                raise FbsInventoryError(
                    "Инвентаризация или перемещение блокирует операции с этим FBS-коробом."
                )
    return _ValidatedBoxLockBatch(
        box_ids=normalized_ids,
        for_execution=bool(for_execution),
        connection_alias=connection.alias,
    )


def assert_box_unlocked(
    box_id: int,
    *,
    for_execution: bool = False,
    validated_batch: _ValidatedBoxLockBatch | None = None,
) -> None:
    if validated_batch is not None:
        connection = transaction.get_connection()
        if (
            not isinstance(validated_batch, _ValidatedBoxLockBatch)
            or not connection.in_atomic_block
            or validated_batch.connection_alias != connection.alias
            or validated_batch.for_execution != bool(for_execution)
            or int(box_id) not in validated_batch.box_ids
        ):
            raise FbsInventoryError("Пакетная проверка этого FBS-короба недействительна.")
        return
    if box_is_locked(box_id, for_execution=for_execution):
        raise FbsInventoryError(
            "Инвентаризация или перемещение блокирует операции с этим FBS-коробом."
        )


def _physical_scope_balances(session: FbsInventorySession) -> QuerySet:
    queryset = FbsStockBalance.objects.select_related(
        "agency",
        "sku_ref",
        "box__pallet__cell__location",
    ).order_by("box__pallet__cell__cell_code", "box__box_code", "sku_code", "id")
    if session.scope_type == FbsInventorySession.SCOPE_ALL:
        return queryset
    if session.scope_type == FbsInventorySession.SCOPE_AGENCY:
        return queryset.filter(agency_id=session.agency_id)
    if session.scope_type == FbsInventorySession.SCOPE_CELL:
        return queryset.filter(box__pallet__cell_id=session.cell_id)
    if session.scope_type == FbsInventorySession.SCOPE_PALLET:
        return queryset.filter(box__pallet_id=session.pallet_id)
    if session.scope_type == FbsInventorySession.SCOPE_BOX:
        return queryset.filter(box_id=session.box_id)
    if session.scope_type == FbsInventorySession.SCOPE_SKU:
        return queryset.filter(agency_id=session.agency_id, sku_ref_id=session.sku_id)
    raise FbsInventoryError("Неизвестная область инвентаризации.")


def _scope_balances(session: FbsInventorySession) -> QuerySet:
    queryset = _physical_scope_balances(session)
    if session.scan_mode == FbsInventorySession.SCAN_MODE_KIZ:
        if session.scope_type == FbsInventorySession.SCOPE_BOX:
            return queryset.filter(
                Q(marking_code__gt="")
                | Q(marking_code="", qty__gt=0, sku_ref__honest_sign=True)
            )
        return queryset.exclude(marking_code="")
    if session.scan_mode == FbsInventorySession.SCAN_MODE_BARCODE:
        if (
            session.managed_workflow
            and session.scope_type == FbsInventorySession.SCOPE_BOX
        ):
            # A shortage-triggered box count is a physical quantity check.  It
            # uses the product barcode even when the stored rows retain their
            # individual KIZ values.
            return queryset
        return queryset.filter(marking_code="")
    raise FbsInventoryError("Неизвестный способ пересчета.")


def _materialize_lines(session: FbsInventorySession) -> None:
    if session.lines.exists():
        return
    balances = list(_scope_balances(session))
    if not balances:
        raise FbsInventoryError("В выбранной области нет товара для этого способа пересчета.")
    protected_quantities = {}
    if session.managed_workflow:
        for payload in session.work_events.filter(action="shortage_reported").order_by("id").values_list("payload", flat=True):
            for balance_id, quantity in payload.get("protected_qty_by_balance", {}).items():
                protected_quantities.setdefault(str(balance_id), int(quantity))
    FbsInventoryLine.objects.bulk_create(
        [
            FbsInventoryLine(
                session=session,
                balance=balance,
                expected_qty=int(balance.qty or 0),
                protected_qty=(
                    protected_quantities.get(str(balance.id), max(int(balance.qty or 0) - int(balance.available_qty or 0) - int(balance.reserved_qty or 0), 0))
                    if session.managed_workflow
                    else 0
                ),
            )
            for balance in balances
        ]
    )


def _create_lock(session: FbsInventorySession, *, block_execution: bool) -> FbsStorageLock:
    return FbsStorageLock.objects.create(
        session=session,
        scope_type=session.scope_type,
        agency=session.agency,
        cell=session.cell,
        pallet=session.pallet,
        box=session.box,
        sku=session.sku,
        block_new_reservations=True,
        block_execution=block_execution,
    )


@transaction.atomic
def create_inventory_session(
    *,
    scope_type: str,
    mode: str = FbsInventorySession.MODE_DRAIN,
    scan_mode: str = FbsInventorySession.SCAN_MODE_BARCODE,
    agency=None,
    cell=None,
    pallet=None,
    box=None,
    sku=None,
    created_by=None,
    managed_workflow: bool = False,
) -> FbsInventorySession:
    _require_writes()
    if mode not in dict(FbsInventorySession.MODE_CHOICES):
        raise FbsInventoryError("Неизвестный режим инвентаризации.")
    if managed_workflow and (mode != FbsInventorySession.MODE_DRAIN or scope_type not in {"box", "cell", "pallet"}):
        raise FbsInventoryError("Назначенная проверка требует точной области и завершения активных операций.")
    session = FbsInventorySession(
        managed_workflow=managed_workflow,
        scope_type=scope_type,
        mode=mode,
        scan_mode=scan_mode,
        agency=agency,
        cell=cell,
        pallet=pallet,
        box=box,
        sku=sku,
        created_by=_authenticated_user(created_by),
    )
    session.full_clean()
    if mode == FbsInventorySession.MODE_IMMEDIATE:
        from .external_issues import assert_no_external_activity
        assert_no_external_activity(_physical_scope_balances(session).values("id"))
    if not _scope_balances(session).exists():
        raise FbsInventoryError("В выбранной области нет товара для этого способа пересчета.")
    if mode == FbsInventorySession.MODE_DRAIN:
        session.status = FbsInventorySession.STATUS_DRAINING
    else:
        session.status = FbsInventorySession.STATUS_COUNTING
        session.started_at = timezone.now()
    session.save()
    if mode != FbsInventorySession.MODE_AUDIT:
        _create_lock(
            session,
            block_execution=mode == FbsInventorySession.MODE_IMMEDIATE,
        )
    if session.status == FbsInventorySession.STATUS_COUNTING:
        _materialize_lines(session)
    return session


def _active_scope_operations(session: FbsInventorySession) -> bool:
    balance_ids = _physical_scope_balances(session).values("id")
    from fbs.models import FbsExternalIssueLine
    if FbsExternalIssueLine.objects.filter(balance_id__in=balance_ids, issue__status__in=("reserved", "picking")).exists():
        return True
    if FbsOrderStockAllocation.objects.filter(
        balance_id__in=balance_ids,
        status__in=(
            FbsOrderStockAllocation.STATUS_RESERVED,
            FbsOrderStockAllocation.STATUS_PICKING,
        ),
        pick_task__status=FbsPickTask.STATUS_IN_PROGRESS,
    ).exists():
        return True
    box_ids = _physical_scope_balances(session).values("box_id")
    if FbsInternalMovement.objects.filter(
        Q(source_box_id__in=box_ids) | Q(target_box_id__in=box_ids),
        status=FbsInternalMovement.STATUS_IN_PROGRESS,
    ).exists():
        return True
    return FbsReplenishmentAllocation.objects.filter(
        target_box_id__in=box_ids,
        status=FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
    ).exists()


@transaction.atomic
def activate_drained_inventory(*, session_id: int) -> FbsInventorySession:
    _require_writes()
    session = FbsInventorySession.objects.select_for_update().get(pk=session_id)
    if session.status == FbsInventorySession.STATUS_COUNTING:
        return session
    if session.status != FbsInventorySession.STATUS_DRAINING:
        raise FbsInventoryError("Инвентаризация не ожидает завершения операций.")
    if _active_scope_operations(session):
        raise FbsInventoryError("В области инвентаризации еще есть активные операции.")
    storage_lock = FbsStorageLock.objects.select_for_update().get(session=session, is_active=True)
    storage_lock.block_execution = True
    storage_lock.save(update_fields=["block_execution", "updated_at"])
    session.status = FbsInventorySession.STATUS_COUNTING
    session.started_at = timezone.now()
    session.save(update_fields=["status", "started_at", "updated_at"])
    _materialize_lines(session)
    return session


def _find_scan_line(
    *,
    session: FbsInventorySession,
    lines: list[FbsInventoryLine],
    scan_code: str,
    count_round: int,
    actor=None,
) -> FbsInventoryLine:
    normalized_scan = normalize_barcode(scan_code)
    if session.scan_mode == FbsInventorySession.SCAN_MODE_KIZ:
        scan_identity = marking_code_identity(scan_code)
        matches = [
            line
            for line in lines
            if line.balance.marking_code
            and marking_code_identity(line.balance.marking_code) == scan_identity
        ]
        if not matches:
            return _find_uncoded_kiz_line(
                session=session,
                lines=lines,
                scan_code=scan_code,
            )
    elif session.scan_mode == FbsInventorySession.SCAN_MODE_BARCODE:
        if session.scope_type == FbsInventorySession.SCOPE_BOX:
            exact_matches = [
                line
                for line in lines
                if normalize_barcode(line.balance.barcode) == normalized_scan
            ]
            marked_matches = [
                line for line in exact_matches if line.balance.marking_code
            ]
            unmarked_matches = [
                line for line in exact_matches if not line.balance.marking_code
            ]
            position_keys = {
                (
                    line.balance.sku_ref_id,
                    str(line.balance.sku_code or "").strip().casefold(),
                    _normalized_size(
                        line.balance.size
                        or getattr(line.balance.sku_ref, "size", "")
                    ),
                    str(line.balance.goods_type or "").strip().casefold(),
                )
                for line in exact_matches
            }
            if marked_matches and len(position_keys) == 1 and len(unmarked_matches) <= 1:
                count_field = (
                    "first_count_qty"
                    if count_round == FbsInventoryScan.ROUND_FIRST
                    else "second_count_qty"
                )
                for line in sorted(marked_matches, key=lambda item: item.id):
                    counted = int(getattr(line, count_field) or 0)
                    if counted < int(line.expected_qty or 0):
                        return line
                if unmarked_matches:
                    # The aggregate row absorbs the unmarked expected quantity
                    # and any surplus; a KIZ row must never become qty > 1.
                    return unmarked_matches[0]
                catalog_barcode = _catalog_barcode_for_box(
                    session=session,
                    scan_code=scan_code,
                )
                return _create_catalog_inventory_line(
                    session=session,
                    catalog_barcode=catalog_barcode,
                    actor=actor,
                )
            if len(exact_matches) == 1:
                return exact_matches[0]
            if len(exact_matches) > 1:
                raise FbsInventoryError(
                    "Штрихкод относится к нескольким строкам остатка. "
                    "Разделите позиции перед инвентаризацией."
                )
            return _find_or_create_catalog_barcode_line(
                session=session,
                lines=lines,
                scan_code=scan_code,
                actor=actor,
            )
        matches = [
            line
            for line in lines
            if normalize_barcode(line.balance.barcode) == normalized_scan
        ]
        if not matches:
            raise FbsInventoryError(
                f"Отсканирован ШК «{scan_code}», но в этой области такого ШК нет. "
                "Новый товар можно добавить только при инвентаризации конкретного короба."
            )
    else:
        raise FbsInventoryError("Неизвестный способ пересчета.")
    if len(matches) > 1:
        raise FbsInventoryError(
            "Штрихкод относится к нескольким строкам остатка. "
            "Выберите более точную область инвентаризации."
        )
    return matches[0]


def _normalized_size(value: object) -> str:
    return str(value or "").strip().casefold()


def _catalog_barcode_for_box(
    *,
    session: FbsInventorySession,
    scan_code: str,
) -> SKUBarcode:
    if session.scope_type != FbsInventorySession.SCOPE_BOX or not session.box_id:
        raise FbsInventoryError(
            "Добавить новую позицию по ШК можно только при инвентаризации "
            "конкретного FBS-короба."
        )
    agency_id = int(session.box.agency_id)
    catalog_rows = list(
        SKUBarcode.objects.select_related("sku")
        .filter(value__iexact=scan_code, sku__deleted=False)
        .order_by("id")
    )
    own_rows = [
        barcode
        for barcode in catalog_rows
        if int(barcode.agency_id or 0) == agency_id
        and int(barcode.sku.agency_id or 0) == agency_id
    ]
    if not own_rows:
        if catalog_rows:
            raise FbsInventoryError(
                f"ШК «{scan_code}» зарегистрирован за другим клиентом. "
                "Добавлять товар другого клиента в этот короб запрещено."
            )
        raise FbsInventoryError(
            f"ШК «{scan_code}» не найден в номенклатуре клиента этого короба. "
            "Товар не добавлен. Отсканируйте точный штрихкод товара без КИЗа."
        )
    if len(own_rows) > 1:
        raise FbsInventoryError(
            f"ШК «{scan_code}» неоднозначно привязан к номенклатуре клиента. "
            "Исправьте карточки SKU перед пересчетом."
        )
    return own_rows[0]


def _line_matches_catalog_sku(
    line: FbsInventoryLine,
    catalog_barcode: SKUBarcode,
) -> bool:
    balance = line.balance
    if balance.sku_ref_id:
        return int(balance.sku_ref_id) == int(catalog_barcode.sku_id)
    return str(balance.sku_code or "").strip().casefold() == str(
        catalog_barcode.sku.sku_code or ""
    ).strip().casefold()


def _matching_catalog_line(
    *,
    lines: list[FbsInventoryLine],
    catalog_barcode: SKUBarcode,
) -> FbsInventoryLine | None:
    sku_lines = [
        line
        for line in lines
        if not str(line.balance.marking_code or "").strip()
        and _line_matches_catalog_sku(line, catalog_barcode)
    ]
    if not sku_lines:
        return None

    exact_matches = [
        line
        for line in sku_lines
        if normalize_barcode(line.balance.barcode) == normalize_barcode(catalog_barcode.value)
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        raise FbsInventoryError(
            "Штрихкод относится к нескольким строкам остатка этого SKU. "
            "Разделите позиции перед инвентаризацией."
        )

    alias_matches = [
        line
        for line in sku_lines
        if same_sku_barcode_variant(
            sku_id=int(catalog_barcode.sku_id),
            first=str(line.balance.barcode or ""),
            second=str(catalog_barcode.value or ""),
        )
    ]
    if len(alias_matches) == 1:
        return alias_matches[0]
    if len(alias_matches) > 1:
        raise FbsInventoryError(
            "Штрихкод относится к нескольким строкам одного SKU и размера. "
            "Разделите позиции перед инвентаризацией."
        )

    scan_size = _normalized_size(catalog_barcode.size or catalog_barcode.sku.size)
    if scan_size:
        size_matches = [
            line
            for line in sku_lines
            if _normalized_size(
                line.balance.size
                or getattr(line.balance.sku_ref, "size", "")
            )
            == scan_size
        ]
        if len(size_matches) == 1:
            return size_matches[0]
        if len(size_matches) > 1:
            raise FbsInventoryError(
                "Штрихкод относится к нескольким строкам одного SKU и размера. "
                "Разделите позиции перед инвентаризацией."
            )
    elif len(sku_lines) == 1:
        return sku_lines[0]
    return None


def _catalog_balance_identity(
    *,
    sku_code: str,
    size: str,
    barcode: str,
    goods_type: str,
) -> str:
    payload = {
        "sku_code": str(sku_code or "").strip(),
        "size": str(size or "").strip(),
        "barcode": str(barcode or "").strip(),
        "goods_type": str(goods_type or "").strip(),
        "marking_code": "",
        "lot_code": "",
        "expiry_date": "",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _create_catalog_inventory_line(
    *,
    session: FbsInventorySession,
    catalog_barcode: SKUBarcode,
    actor,
) -> FbsInventoryLine:
    sku = catalog_barcode.sku
    size = str(catalog_barcode.size or sku.size or "").strip()[:64]
    goods_type = str(sku.type_tovar or "").strip()[:64]
    barcode = str(catalog_barcode.value or "").strip()
    identity_key = _catalog_balance_identity(
        sku_code=sku.sku_code,
        size=size,
        barcode=barcode,
        goods_type=goods_type,
    )
    balance = (
        FbsStockBalance.objects.select_for_update()
        .filter(box_id=session.box_id, identity_key=identity_key)
        .first()
    )
    balance_created = balance is None
    if balance is None:
        balance = FbsStockBalance(
            agency_id=session.box.agency_id,
            box_id=session.box_id,
            sku_ref=sku,
            identity_key=identity_key,
            sku_code=str(sku.sku_code or "").strip(),
            name=str(sku.name or "").strip(),
            size=size,
            barcode=barcode,
            goods_type=goods_type,
            marking_code="",
            lot_code="",
            expiry_date=None,
            qty=0,
            available_qty=0,
            reserved_qty=0,
            external_reserved_qty=0,
        )
        balance.full_clean()
        balance.save()
    elif (
        int(balance.agency_id) != int(session.box.agency_id)
        or balance.sku_ref_id != sku.id
        or normalize_barcode(balance.barcode) != normalize_barcode(barcode)
    ):
        raise FbsInventoryError(
            "Новая позиция совпала с другой строкой остатка. "
            "Товар не добавлен; требуется проверка данных короба."
        )

    line, line_created = FbsInventoryLine.objects.get_or_create(
        session=session,
        balance=balance,
        defaults={
            "expected_qty": int(balance.qty or 0),
            "protected_qty": 0,
        },
    )
    if line_created:
        FbsInventoryWorkEvent.objects.create(
            session=session,
            action="catalog_position_discovered",
            actor=actor,
            payload={
                "balance_id": balance.id,
                "balance_created": balance_created,
                "sku_id": sku.id,
                "barcode": barcode,
                "expected_qty": int(balance.qty or 0),
            },
        )
    return line


def _find_or_create_catalog_barcode_line(
    *,
    session: FbsInventorySession,
    lines: list[FbsInventoryLine],
    scan_code: str,
    actor,
) -> FbsInventoryLine:
    catalog_barcode = _catalog_barcode_for_box(
        session=session,
        scan_code=scan_code,
    )
    line = _matching_catalog_line(
        lines=lines,
        catalog_barcode=catalog_barcode,
    )
    if line is not None:
        return line
    return _create_catalog_inventory_line(
        session=session,
        catalog_barcode=catalog_barcode,
        actor=actor,
    )


def _barcode_as_gtin14(value: str) -> str:
    barcode = normalize_barcode(value)
    if not barcode.isdigit() or len(barcode) not in {8, 12, 13, 14}:
        return ""
    return barcode.zfill(14)


def _marking_gtin(scan_code: str) -> tuple[str, str]:
    try:
        normalized = validate_import_marking_code(scan_code)
    except MarkingCodeFormatError as exc:
        raise FbsInventoryError(f"Некорректный КИЗ: {exc}.") from exc
    if not normalized.startswith("01") or normalized[16:18] != "21":
        raise FbsInventoryError(
            "Отсканируйте точный КИЗ. Новый КИЗ должен быть полным Data Matrix: "
            "01 + GTIN-14 + 21 + серийный номер."
        )
    return normalized, normalized[2:16]


def _possible_existing_kiz_balances(*, agency_id: int, scan_code: str):
    identity = marking_code_identity(scan_code)
    variants = marking_code_variants(scan_code)
    lookup = Q(marking_code__in=variants)
    if identity:
        lookup |= Q(marking_code__startswith=identity)
        lookup |= Q(marking_code__startswith=f"]d2{identity}")
        lookup |= Q(marking_code__startswith=f"]D2{identity}")
    return FbsStockBalance.objects.filter(
        agency_id=agency_id,
    ).exclude(marking_code="").filter(lookup).select_related("box").only(
        "id", "box_id", "box__box_code", "marking_code", "sku_code"
    )


def _find_uncoded_kiz_line(
    *,
    session: FbsInventorySession,
    lines: list[FbsInventoryLine],
    scan_code: str,
) -> FbsInventoryLine:
    if session.scope_type != FbsInventorySession.SCOPE_BOX:
        raise FbsInventoryError(
            "Новый КИЗ можно зарегистрировать только при инвентаризации конкретного FBS-короба."
        )
    normalized, scanned_gtin = _marking_gtin(scan_code)
    scan_identity = marking_code_identity(normalized)
    agency_id = int(session.box.agency_id)
    for balance in _possible_existing_kiz_balances(
        agency_id=agency_id,
        scan_code=normalized,
    ):
        if marking_code_identity(balance.marking_code) != scan_identity:
            continue
        if balance.box_id == session.box_id:
            raise FbsInventoryError(
                "ДУБЛЬ КИЗа: этот код уже зарегистрирован в проверяемом FBS-коробе. "
                "Повторный скан не добавлен."
            )
        raise FbsInventoryError(
            f"ДУБЛЬ КИЗа: этот код уже числится в другом FBS-коробе "
            f"{balance.box.box_code}. Повторный скан не добавлен; сначала оформите перемещение."
        )

    candidates = [
        line
        for line in lines
        if not str(line.balance.marking_code or "").strip()
        and int(line.balance.qty or 0) > 0
        and line.balance.sku_ref_id
        and bool(getattr(line.balance.sku_ref, "honest_sign", False))
    ]
    aliases_by_sku: dict[int, set[str]] = {}
    for sku_id, barcode in SKUBarcode.objects.filter(
        sku_id__in={line.balance.sku_ref_id for line in candidates}
    ).values_list("sku_id", "value"):
        aliases_by_sku.setdefault(int(sku_id), set()).add(str(barcode or ""))
    matches = []
    for line in candidates:
        barcodes = aliases_by_sku.get(int(line.balance.sku_ref_id), set()) | {
            str(line.balance.barcode or "")
        }
        if scanned_gtin in {_barcode_as_gtin14(barcode) for barcode in barcodes}:
            matches.append(line)
    if not matches:
        raise FbsInventoryError(
            "КИЗ относится к другому товару или его GTIN не привязан к SKU этого короба."
        )
    if len(matches) > 1:
        raise FbsInventoryError(
            "GTIN КИЗа относится к нескольким партиям товара в коробе. "
            "Разделите партии перед покодовой инвентаризацией."
        )
    return matches[0]


def _inventory_scan_identity_exists(
    *, session: FbsInventorySession, count_round: int, scan_code: str
) -> bool:
    identity = marking_code_identity(scan_code)
    variants = marking_code_variants(scan_code)
    lookup = Q(scan_code__in=variants)
    if identity:
        lookup |= Q(scan_code__startswith=identity)
        lookup |= Q(scan_code__startswith=f"]d2{identity}")
        lookup |= Q(scan_code__startswith=f"]D2{identity}")
    return any(
        marking_code_identity(value) == identity
        for value in FbsInventoryScan.objects.filter(
            line__session=session,
            count_round=count_round,
        ).filter(lookup).values_list("scan_code", flat=True)
    )


@transaction.atomic
def record_inventory_scan(
    *,
    session_id: int,
    scan_code: str,
    counted_by,
    qty: int = 1,
    box_scan: str = "",
) -> FbsInventoryLine:
    _require_writes()
    actor = _authenticated_user(counted_by)
    if actor is None:
        raise FbsInventoryError("Не указан сотрудник, выполняющий пересчет.")
    scan_code = str(scan_code or "").strip()
    if not scan_code:
        raise FbsInventoryError("Пустой скан не допускается.")
    if int(qty or 0) != 1:
        raise FbsInventoryError("Количество вводится сканированием по одной единице.")
    session = FbsInventorySession.objects.select_for_update().get(pk=session_id)
    if session.status not in {
        FbsInventorySession.STATUS_COUNTING,
        FbsInventorySession.STATUS_RECOUNT,
    }:
        raise FbsInventoryError("Инвентаризация сейчас не принимает сканы.")
    if session.managed_workflow:
        from .inventory_workflow import assert_counter
        assert_counter(session, actor)
    count_round = (
        FbsInventoryScan.ROUND_FIRST
        if session.status == FbsInventorySession.STATUS_COUNTING
        else FbsInventoryScan.ROUND_SECOND
    )
    if count_round == FbsInventoryScan.ROUND_FIRST:
        if session.first_counter_id not in {None, actor.id}:
            raise FbsInventoryError("Первый пересчет уже выполняет другой сотрудник.")
        session.first_counter = actor
        session.save(update_fields=["first_counter", "updated_at"])
    else:
        from .inventory_workflow import same_counter_recount_allowed
        if (
            session.first_counter_id == actor.id
            and not same_counter_recount_allowed(session, actor)
        ):
            raise FbsInventoryError("Повторный пересчет должен выполнить другой сотрудник.")
        if session.second_counter_id not in {None, actor.id}:
            raise FbsInventoryError("Повторный пересчет уже выполняет другой сотрудник.")
        session.second_counter = actor
        session.save(update_fields=["second_counter", "updated_at"])

    lines = list(
        FbsInventoryLine.objects.select_for_update(of=("self",))
        .select_related("balance__sku_ref")
        .filter(session=session)
    )
    requires_box_scan = (
        session.managed_workflow
        and session.scope_type != FbsInventorySession.SCOPE_BOX
        and not (
            session.scope_type == FbsInventorySession.SCOPE_CELL
            and session.scan_mode == FbsInventorySession.SCAN_MODE_KIZ
        )
    )
    if requires_box_scan:
        lines = [line for line in lines if line.balance.box.box_code.casefold() == str(box_scan).strip().casefold()]
        if not lines:
            raise FbsInventoryError("Сначала отсканируйте короб из проверяемой области.")
    if session.scan_mode == FbsInventorySession.SCAN_MODE_KIZ and _inventory_scan_identity_exists(
        session=session,
        count_round=count_round,
        scan_code=scan_code,
    ):
        raise FbsInventoryError(
            "ДУБЛЬ КИЗа: этот код уже отсканирован в текущем пересчете. "
            "Повторный скан не добавлен."
        )
    line = _find_scan_line(
        session=session,
        lines=lines,
        scan_code=scan_code,
        count_round=count_round,
        actor=actor,
    )
    if session.scan_mode == FbsInventorySession.SCAN_MODE_KIZ:
        scan_code = normalize_marking_code(scan_code)
    FbsInventoryScan.objects.create(
        line=line,
        count_round=count_round,
        scan_code=scan_code,
        qty=1,
        counted_by=actor,
    )
    field = "first_count_qty" if count_round == FbsInventoryScan.ROUND_FIRST else "second_count_qty"
    setattr(line, field, int(getattr(line, field) or 0) + 1)
    line.save(update_fields=[field, "updated_at"])
    return line


@transaction.atomic
def finish_inventory_count(*, session_id: int, counted_by, confirm_complete: bool = False) -> FbsInventorySession:
    _require_writes()
    actor = _authenticated_user(counted_by)
    session = FbsInventorySession.objects.select_for_update().get(pk=session_id)
    if session.managed_workflow:
        from .inventory_workflow import assert_counter, event
        assert_counter(session, actor)
        if not confirm_complete:
            raise FbsInventoryError("Подтвердите полный пересчет области, включая отсутствующий товар (0 шт.).")
    if session.status == FbsInventorySession.STATUS_COUNTING:
        if actor is None or session.first_counter_id != actor.id:
            raise FbsInventoryError("Первый пересчет может завершить только его исполнитель.")
        lines = list(FbsInventoryLine.objects.select_for_update().filter(session=session))
        for line in lines:
            if line.first_count_qty is None:
                line.first_count_qty = 0
        FbsInventoryLine.objects.bulk_update(lines, ["first_count_qty", "updated_at"])
        has_discrepancy = any(line.first_count_qty != line.expected_qty for line in lines)
        session.status = (
            FbsInventorySession.STATUS_RECOUNT
            if has_discrepancy
            else FbsInventorySession.STATUS_APPROVAL
        )
    elif session.status == FbsInventorySession.STATUS_RECOUNT:
        if actor is None or session.second_counter_id != actor.id:
            raise FbsInventoryError("Повторный пересчет может завершить только его исполнитель.")
        lines = list(FbsInventoryLine.objects.select_for_update().filter(session=session))
        for line in lines:
            if line.second_count_qty is None:
                line.second_count_qty = 0
        FbsInventoryLine.objects.bulk_update(lines, ["second_count_qty", "updated_at"])
        session.status = FbsInventorySession.STATUS_APPROVAL
    else:
        raise FbsInventoryError("Пересчет нельзя завершить в текущем статусе.")
    session.save(update_fields=["status", "updated_at"])
    if session.managed_workflow:
        event(session, "count_finished", actor, status=session.status, zero_confirmed=True)
    return session


@transaction.atomic
def approve_inventory(
    *,
    session_id: int,
    approved_by,
    final_counts: dict[int, int] | None = None,
) -> FbsInventorySession:
    _require_writes()
    actor = _authenticated_user(approved_by)
    if actor is None:
        raise FbsInventoryError("Не указан руководитель, утверждающий инвентаризацию.")
    session = FbsInventorySession.objects.select_for_update().get(pk=session_id)
    if session.managed_workflow:
        from .inventory_workflow import require_manager
        require_manager(actor, approve=True)
    if session.status != FbsInventorySession.STATUS_APPROVAL:
        raise FbsInventoryError("Инвентаризация еще не готова к утверждению.")
    final_counts = final_counts or {}
    affected_order_ids = set()
    if session.managed_workflow:
        from .inventory_workflow import prepare_inventory_approval
        affected_order_ids = prepare_inventory_approval(session, actor, final_counts)
    lines = list(
        FbsInventoryLine.objects.select_for_update(of=("self",))
        .select_related("balance__sku_ref", "balance__box__pallet__cell__location")
        .filter(session=session)
    )
    now = timezone.now()
    for line in lines:
        first = int(line.first_count_qty or 0)
        second = line.second_count_qty
        captures_new_kiz_line = bool(
            session.scan_mode == FbsInventorySession.SCAN_MODE_KIZ
            and session.scope_type == FbsInventorySession.SCOPE_BOX
            and not str(line.balance.marking_code or "").strip()
            and line.balance.sku_ref_id
            and bool(getattr(line.balance.sku_ref, "honest_sign", False))
        )
        if captures_new_kiz_line and second is not None:
            final_qty = int(second)
        elif second is None or int(second) == first:
            final_qty = first
        elif line.id in final_counts:
            final_qty = int(final_counts[line.id])
        else:
            raise FbsInventoryError(
                f"По строке {line.id} два пересчета расходятся; укажите итог руководителя."
            )
        if final_qty < 0:
            raise FbsInventoryError("Итоговый остаток не может быть отрицательным.")
        balance = FbsStockBalance.objects.select_for_update().get(pk=line.balance_id)
        from .external_issues import assert_no_external_activity
        assert_no_external_activity([balance.pk])
        captures_new_kiz = captures_new_kiz_line
        if captures_new_kiz:
            approved_round = (
                FbsInventoryScan.ROUND_SECOND
                if line.second_count_qty is not None
                else FbsInventoryScan.ROUND_FIRST
            )
            scanned_codes = [
                normalize_marking_code(value)
                for value in line.scans.filter(
                    count_round=approved_round,
                ).order_by("id").values_list("scan_code", flat=True)
            ]
            if final_qty != len(scanned_codes):
                raise FbsInventoryError(
                    f"По строке {line.id} итог должен совпадать с количеством "
                    "фактически отсканированных КИЗов."
                )
            approved_delta = final_qty - int(balance.qty or 0)
            if session.mode != FbsInventorySession.MODE_AUDIT:
                _materialize_inventory_kiz_balances(
                    session=session,
                    line=line,
                    source=balance,
                    scanned_codes=scanned_codes,
                    actor=actor,
                    now=now,
                )
            line.final_qty = final_qty
            line.approved_delta = approved_delta
            continue
        if final_qty < int(balance.reserved_qty or 0):
            raise FbsInventoryError("Фактический остаток меньше уже зарезервированного количества.")
        delta = final_qty - int(balance.qty or 0)
        line.final_qty = final_qty
        line.approved_delta = delta
        if session.mode != FbsInventorySession.MODE_AUDIT and (delta or session.managed_workflow):
            old_qty = int(balance.qty or 0)
            balance.qty = final_qty
            balance.available_qty = max(final_qty - int(balance.reserved_qty or 0) - int(line.protected_qty or 0), 0)
            balance.save(update_fields=["qty", "available_qty", "updated_at"])
            if delta:
                _record_inventory_adjustment(session, balance, actor, now, old_qty, final_qty, delta)
    FbsInventoryLine.objects.bulk_update(lines, ["final_qty", "approved_delta", "updated_at"])
    FbsStorageLock.objects.filter(session=session, is_active=True).update(
        is_active=False,
        released_at=now,
        updated_at=now,
    )
    session.status = FbsInventorySession.STATUS_DONE
    session.approved_by = actor
    session.completed_at = now
    session.save(update_fields=["status", "approved_by", "completed_at", "updated_at"])
    if session.managed_workflow:
        from .inventory_workflow import event, retry_inventory_orders
        event(session, "approved", actor)
        retry_inventory_orders(session, actor, affected_order_ids)
    return session


@transaction.atomic
def confirm_inventory_discrepancies(
    *,
    session_id: int,
    confirmed_by,
    responsibility_acknowledged: bool = False,
) -> FbsInventorySession:
    """Accept the first count and finish a managed inventory without round two.

    This is an explicit warehouse-manager/storekeeper responsibility path.  It
    is available only before the repeat count starts and then uses the normal
    approval write path, including reserve protection and order follow-up.
    """
    _require_writes()
    actor = _authenticated_user(confirmed_by)
    if actor is None:
        raise FbsInventoryError("Не указан сотрудник, подтверждающий расхождения.")
    from .inventory_workflow import event, require_manager

    require_manager(actor, approve=True)
    if not responsibility_acknowledged:
        raise FbsInventoryError("Подтвердите ответственность за расхождения.")

    session = FbsInventorySession.objects.select_for_update().get(pk=session_id)
    if not session.managed_workflow or session.status != FbsInventorySession.STATUS_RECOUNT:
        raise FbsInventoryError("Подтверждение доступно только после первого пересчета с расхождениями.")
    if session.recount_started_at:
        raise FbsInventoryError("Повторный пересчет уже начат. Завершите его текущим исполнителем.")

    lines = list(FbsInventoryLine.objects.select_for_update().filter(session=session).order_by("id"))
    if not lines or any(line.first_count_qty is None for line in lines):
        raise FbsInventoryError("Первый пересчет не завершен полностью.")
    expected_total = sum(int(line.expected_qty or 0) for line in lines)
    confirmed_total = sum(int(line.first_count_qty or 0) for line in lines)
    if expected_total == confirmed_total and all(
        int(line.expected_qty or 0) == int(line.first_count_qty or 0)
        for line in lines
    ):
        raise FbsInventoryError("В первом пересчете нет расхождений для подтверждения.")

    session.status = FbsInventorySession.STATUS_APPROVAL
    session.save(update_fields=["status", "updated_at"])
    event(
        session,
        "first_count_confirmed",
        actor,
        responsibility_acknowledged=True,
        expected_total=expected_total,
        confirmed_total=confirmed_total,
        delta=confirmed_total - expected_total,
        first_counter_id=session.first_counter_id,
        repeat_count_waived=True,
    )
    return approve_inventory(session_id=session.id, approved_by=actor)


def _balance_identity_for_marking(balance: FbsStockBalance, marking_code: str) -> str:
    payload = {
        "sku_code": str(balance.sku_code or "").strip(),
        "size": str(balance.size or "").strip(),
        "barcode": str(balance.barcode or "").strip(),
        "goods_type": str(balance.goods_type or "").strip(),
        "marking_code": normalize_marking_code(marking_code),
        "lot_code": str(balance.lot_code or "").strip(),
        "expiry_date": balance.expiry_date.isoformat() if balance.expiry_date else "",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _materialize_inventory_kiz_balances(
    *,
    session: FbsInventorySession,
    line: FbsInventoryLine,
    source: FbsStockBalance,
    scanned_codes: list[str],
    actor,
    now,
) -> None:
    old_qty = int(source.qty or 0)
    if int(source.reserved_qty or 0) or int(line.protected_qty or 0):
        raise FbsInventoryError(
            "В обезличенном остатке есть активный резерв. Завершите или снимите резерв до привязки КИЗов."
        )
    if int(source.available_qty or 0) != old_qty:
        raise FbsInventoryError(
            "Обезличенный остаток занят другой операцией. Повторите утверждение после освобождения товара."
        )
    identities = [marking_code_identity(code) for code in scanned_codes]
    if len(set(identities)) != len(identities):
        raise FbsInventoryError("В итоговом пересчете обнаружены повторяющиеся КИЗы.")
    for code, identity in zip(scanned_codes, identities):
        for existing in _possible_existing_kiz_balances(
            agency_id=source.agency_id,
            scan_code=code,
        ).select_for_update():
            if marking_code_identity(existing.marking_code) == identity:
                raise FbsInventoryError(
                    "ДУБЛЬ КИЗа: один из кодов уже зарегистрирован в FBS-остатках. "
                    "Обновите инвентаризацию."
                )

    final_qty = len(scanned_codes)
    source.qty = 0
    source.available_qty = 0
    source.reserved_qty = 0
    source.save(update_fields=["qty", "available_qty", "reserved_qty", "updated_at"])
    FbsStockBalance.objects.bulk_create(
        [
            FbsStockBalance(
                agency=source.agency,
                box=source.box,
                sku_ref=source.sku_ref,
                identity_key=_balance_identity_for_marking(source, code),
                sku_code=source.sku_code,
                name=source.name,
                size=source.size,
                barcode=source.barcode,
                goods_type=source.goods_type,
                marking_code=code,
                lot_code=source.lot_code,
                expiry_date=source.expiry_date,
                qty=1,
                available_qty=1,
                reserved_qty=0,
            )
            for code in scanned_codes
        ]
    )
    delta = final_qty - old_qty
    if delta:
        _record_inventory_adjustment(
            session,
            source,
            actor,
            now,
            old_qty,
            final_qty,
            delta,
        )
    if final_qty:
        WarehouseEvent.objects.create(
            agency=source.agency,
            event_type="fbs_inventory_kiz_registered",
            stock_context_type="fbs_inventory",
            stock_context_id=str(session.id),
            container=source.box.source_container,
            source_document_type="fbs_inventory",
            source_document_id=str(session.id),
            from_location=source.box.pallet.cell.location,
            to_location=source.box.pallet.cell.location,
            from_zone_code=str(source.box.pallet.cell.location.zone_code or ""),
            to_zone_code=str(source.box.pallet.cell.location.zone_code or ""),
            qty=final_qty,
            payload={
                "source_balance_id": source.id,
                "registered_kiz_qty": final_qty,
                "old_qty": old_qty,
                "new_qty": final_qty,
                "delta": delta,
                "box_code": source.box.box_code,
            },
            performed_by=actor,
            performed_by_role="head_manager",
            occurred_at=now,
        )


def _record_inventory_adjustment(session, balance, actor, now, old_qty, final_qty, delta):
    WarehouseEvent.objects.create(
                agency=balance.agency,
                event_type="fbs_inventory_adjusted",
                stock_context_type="fbs_inventory",
                stock_context_id=str(session.id),
                container=balance.box.source_container,
                source_document_type="fbs_inventory",
                source_document_id=str(session.id),
                from_location=balance.box.pallet.cell.location,
                to_location=balance.box.pallet.cell.location,
                from_zone_code=str(balance.box.pallet.cell.location.zone_code or ""),
                to_zone_code=str(balance.box.pallet.cell.location.zone_code or ""),
                qty=abs(delta),
                payload={
                    "balance_id": balance.id,
                    "old_qty": old_qty,
                    "new_qty": final_qty,
                    "delta": delta,
                    "mode": session.mode,
                },
                performed_by=actor,
                performed_by_role="head_manager",
                occurred_at=now,
            )


@transaction.atomic
def cancel_inventory(*, session_id: int) -> FbsInventorySession:
    _require_writes()
    session = FbsInventorySession.objects.select_for_update().get(pk=session_id)
    if session.managed_workflow and session.pick_issues.exists():
        raise FbsInventoryError("Проверку недостачи нельзя отменить без подтвержденного результата.")
    if session.status == FbsInventorySession.STATUS_DONE:
        raise FbsInventoryError("Завершенную инвентаризацию нельзя отменить.")
    now = timezone.now()
    FbsStorageLock.objects.filter(session=session, is_active=True).update(
        is_active=False,
        released_at=now,
        updated_at=now,
    )
    session.status = FbsInventorySession.STATUS_CANCELED
    session.completed_at = now
    session.save(update_fields=["status", "completed_at", "updated_at"])
    return session
