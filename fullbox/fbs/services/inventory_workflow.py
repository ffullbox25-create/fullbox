"""Assigned, address-confirmed counts following a picker shortage.

No physical adjustment occurs on a shortage report or on a count. Approval
uses the existing inventory write path; picked goods are never released here.
"""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from employees.models import Employee
from fbs.exceptions import FbsInventoryError, FbsError
from fbs.models import (
    FbsBox, FbsInventorySession, FbsInventoryWorkEvent, FbsPallet, FbsPickException,
    FbsOrder, FbsOrderStockAllocation, FbsPickTask, FbsStorageCell, FbsStorageLock,
    FbsStockBalance,
)

MANAGERS = {"storekeeper", "head_manager", "director", "admin", "developer"}
APPROVERS = MANAGERS
COUNTERS = MANAGERS | {"picker", "fbs_picker", "fbs_controller"}
ASSIGNABLE_COUNTER_ROLES = {"picker"}
ASSIGNABLE_INVENTORY_SCOPES = {
    FbsInventorySession.SCOPE_BOX,
    FbsInventorySession.SCOPE_CELL,
    FbsInventorySession.SCOPE_PALLET,
}
OPEN_STATUSES = ("planned", "draining", "counting", "recount", "approval")
SAME_COUNTER_RECOUNT_APPROVED = "same_counter_recount_approved"


def has_inventory_role(user, *, approve=False):
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.is_superuser or user.username == "dev":
        return True
    return Employee.objects.filter(user=user, is_active=True, role__in=APPROVERS if approve else MANAGERS).exists()


def require_manager(user, *, approve=False):
    if not has_inventory_role(user, approve=approve):
        raise FbsInventoryError("Утверждение доступно руководителю." if approve else "Назначение доступно кладовщику.")


def event(session, action, actor, **payload):
    FbsInventoryWorkEvent.objects.create(session=session, action=action, actor=actor, payload=payload)


def same_counter_recount_allowed(session, user) -> bool:
    """Return whether a manager explicitly approved this one-session exception."""
    user_id = int(getattr(user, "id", 0) or 0)
    if (
        not user_id
        or session.status != FbsInventorySession.STATUS_RECOUNT
        or session.first_counter_id != user_id
    ):
        return False
    return session.work_events.filter(
        action=SAME_COUNTER_RECOUNT_APPROVED,
        payload__user_id=user_id,
    ).exists()


@transaction.atomic
def approve_same_counter_recount(*, session_id, assigned_to, approved_by, reason):
    """Authorize the first counter to repeat one specific inventory session."""
    from .inventory import _require_writes

    _require_writes()
    require_manager(approved_by, approve=True)
    session = FbsInventorySession.objects.select_for_update().get(pk=session_id)
    if session.status != FbsInventorySession.STATUS_RECOUNT:
        raise FbsInventoryError("Одноразовое разрешение доступно только для повторного пересчета.")
    if session.recount_started_at:
        raise FbsInventoryError("Повторный пересчет уже начат.")
    if not assigned_to or session.first_counter_id != assigned_to.id:
        raise FbsInventoryError("Разрешение относится только к сотруднику первого пересчета.")
    if not assigned_to.is_active or not Employee.objects.filter(
        user=assigned_to,
        is_active=True,
        role__in=ASSIGNABLE_COUNTER_ROLES,
    ).exists():
        raise FbsInventoryError("Выберите активную учетную запись сборщика FBS.")
    if session.pick_issues.filter(created_by=assigned_to).exists():
        raise FbsInventoryError("Проверку не может выполнять сотрудник, сообщивший о недостаче.")
    reason = str(reason or "").strip()
    if not reason:
        raise FbsInventoryError("Укажите причину одноразового разрешения.")

    already_approved = same_counter_recount_allowed(session, assigned_to)
    if not already_approved:
        event(
            session,
            SAME_COUNTER_RECOUNT_APPROVED,
            approved_by,
            user_id=assigned_to.id,
            reason=reason,
            first_total=sum(
                int(qty or 0)
                for qty in session.lines.values_list("first_count_qty", flat=True)
            ),
        )
    if session.recount_assigned_to_id != assigned_to.id:
        session.recount_assigned_to = assigned_to
        session.managed_workflow = True
        session.save(
            update_fields=["recount_assigned_to", "managed_workflow", "updated_at"]
        )
        event(
            session,
            "assigned",
            approved_by,
            round=2,
            user_id=assigned_to.id,
            same_counter_override=True,
        )
    return session


@transaction.atomic
def enqueue_shortage_inventory(*, issues, reported_by, protected_quantities):
    from .inventory import create_inventory_session
    sessions = {}
    for issue in issues:
        balance = issue.allocation.balance
        key = balance.box_id
        if key not in sessions:
            # Serializes simultaneous reports about the same physical box.
            box = FbsBox.objects.select_for_update().get(pk=balance.box_id)
            # A shortage inventory verifies physical quantity by the product
            # barcode.  KIZ is not requested in this workflow, including for
            # goods whose stock rows keep individual marking codes.
            scan_mode = FbsInventorySession.SCAN_MODE_BARCODE
            session = FbsInventorySession.objects.filter(
                managed_workflow=True, scope_type="box", box=box,
                status__in=OPEN_STATUSES,
            ).order_by("id").first()
            if session is None:
                session = create_inventory_session(
                    scope_type="box", mode="drain", scan_mode=scan_mode,
                    box=box, agency=box.agency, created_by=reported_by,
                    managed_workflow=True,
                )
            sessions[key] = session
        session = sessions[key]
        issue.inventory_session = session
        issue.save(update_fields=["inventory_session", "updated_at"])
        event(session, "shortage_reported", reported_by, issue_id=issue.id,
              order_id=issue.task.order_id, allocation_id=issue.allocation_id,
              protected_qty_by_balance=protected_quantities)
    return tuple(sessions.values())


AGENCY_BATCH_MAX_PALLETS = 25


@transaction.atomic
def create_agency_inventory_batch(*, agency, scan_mode, created_by, limit=10):
    """Задание кладовщика на инвентаризацию клиента — пакет проверок по паллетам.

    Область клиента намеренно не берётся одним куском: замок со scope_type
    "agency" останавливает новые резервы по всему остатку клиента, то есть
    останавливает его отгрузку целиком. Паллета принадлежит одному клиенту
    (FbsPallet.agency), поэтому пакет поячеечных заданий даёт тот же охват,
    но блокирует ровно ту паллету, которую сейчас считают.
    """
    from .inventory import _require_writes, create_inventory_session

    _require_writes()
    require_manager(created_by)
    if agency is None:
        raise FbsInventoryError("Выберите клиента.")
    if scan_mode not in dict(FbsInventorySession.SCAN_MODE_CHOICES):
        raise FbsInventoryError("Неизвестный способ пересчета.")
    try:
        limit = int(limit or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        raise FbsInventoryError("Укажите, сколько паллет взять в работу.")
    limit = min(limit, AGENCY_BATCH_MAX_PALLETS)

    busy_pallet_ids = set(
        FbsInventorySession.objects.filter(
            scope_type=FbsInventorySession.SCOPE_PALLET,
            status__in=OPEN_STATUSES,
            pallet__isnull=False,
        ).values_list("pallet_id", flat=True)
    )
    # Паллеты клиента с остатком, по маршруту обхода: ячейка, затем код паллеты.
    pallet_ids = list(
        FbsStockBalance.objects.filter(agency=agency, qty__gt=0, box__pallet__isnull=False)
        .exclude(box__pallet__status=FbsPallet.STATUS_ARCHIVED)
        .order_by("box__pallet__cell__cell_code", "box__pallet__pallet_code", "box__pallet_id")
        .values_list("box__pallet_id", flat=True)
    )
    ordered_unique_ids = list(dict.fromkeys(pallet_ids))

    created = []
    skipped_busy = 0
    skipped_empty = 0
    for pallet_id in ordered_unique_ids:
        if len(created) >= limit:
            break
        if pallet_id in busy_pallet_ids:
            skipped_busy += 1
            continue
        pallet = FbsPallet.objects.filter(pk=pallet_id).first()
        if pallet is None:
            continue
        try:
            # Вложенная точка сохранения: паллета без товара под выбранный
            # способ пересчета не должна отменять уже созданные задания.
            with transaction.atomic():
                session = create_inventory_session(
                    scope_type=FbsInventorySession.SCOPE_PALLET,
                    mode=FbsInventorySession.MODE_DRAIN,
                    scan_mode=scan_mode,
                    pallet=pallet,
                    agency=pallet.agency or agency,
                    created_by=created_by,
                    managed_workflow=True,
                )
        except FbsInventoryError:
            skipped_empty += 1
            continue
        event(
            session,
            "agency_batch_created",
            created_by,
            agency_id=int(getattr(agency, "id", 0) or 0),
            pallet_id=int(pallet_id),
            scan_mode=scan_mode,
        )
        created.append(session)

    if not created:
        raise FbsInventoryError(
            "Нет свободных паллет клиента для этого способа пересчета: "
            f"уже проверяется {skipped_busy}, без подходящего товара {skipped_empty}."
        )
    return created, skipped_busy, skipped_empty


@transaction.atomic
def assign_inventory(*, session_id, assigned_to, assigned_by):
    from .inventory import _require_writes
    _require_writes()
    require_manager(assigned_by)
    session = FbsInventorySession.objects.select_for_update().get(pk=session_id)
    if session.status not in ("planned", "draining", "counting", "recount"):
        raise FbsInventoryError("Пересчет уже завершен.")
    if session.scope_type not in ASSIGNABLE_INVENTORY_SCOPES:
        raise FbsInventoryError(
            "Назначить на ТСД можно только короб, паллету или ячейку. "
            "Для проверки клиента создайте пакет заданий по паллетам."
        )
    if not assigned_to or not assigned_to.is_active or not Employee.objects.filter(
        user=assigned_to, is_active=True, role__in=ASSIGNABLE_COUNTER_ROLES,
    ).exists():
        raise FbsInventoryError("Выберите активную учетную запись сборщика FBS.")
    second = session.status == "recount"
    if not session.managed_workflow and session.first_counter_id:
        raise FbsInventoryError("Пересчет уже начат по прежнему порядку. Завершите его текущим исполнителем.")
    if session.recount_started_at if second else session.count_started_at:
        raise FbsInventoryError("Пересчет начат. Смена исполнителя без нового пересчета запрещена.")
    if (
        second
        and session.first_counter_id == assigned_to.id
        and not same_counter_recount_allowed(session, assigned_to)
    ):
        raise FbsInventoryError("Повторный пересчет должен выполнить другой сотрудник.")
    if session.pick_issues.filter(created_by=assigned_to).exists():
        raise FbsInventoryError("Проверку должен выполнить не тот сотрудник, который сообщил о недостаче.")
    field = "recount_assigned_to" if second else "assigned_to"
    setattr(session, field, assigned_to)
    session.managed_workflow = True
    session.save(update_fields=[field, "managed_workflow", "updated_at"])
    event(session, "assigned", assigned_by, round=2 if second else 1, user_id=assigned_to.id)
    return session


@transaction.atomic
def start_self_kiz_inventory(
    *,
    counted_by,
    target_type="",
    target_scan="",
    place_scan="",
    box_scan="",
):
    """Let a picker start a KIZ count for one scanned box or one scanned cell.

    ``place_scan`` plus ``box_scan`` remains supported for older callers. The
    TSD uses ``target_type`` plus ``target_scan`` so the picker confirms only
    the selected physical scope.
    """
    from .inventory import (
        _require_writes,
        assert_box_unlocked,
        create_inventory_session,
        validate_boxes_unlocked,
    )

    _require_writes()
    actor = counted_by if getattr(counted_by, "is_authenticated", False) else None
    if actor is None or not actor.is_active or not Employee.objects.filter(
        user=actor,
        is_active=True,
        role="picker",
    ).exists():
        raise FbsInventoryError("Самостоятельную инвентаризацию ЧЗ может начать только подборщик.")

    active_session = (
        FbsInventorySession.objects.select_for_update()
        .filter(
            Q(assigned_to=actor, status__in=("planned", "draining", "counting"))
            | Q(recount_assigned_to=actor, status="recount")
        )
        .order_by("id")
        .first()
    )
    if active_session is not None:
        raise FbsInventoryError(
            f"Сначала завершите назначенную инвентаризацию #{active_session.id}."
        )

    normalized_target_type = str(target_type or "").strip().casefold()
    legacy_pair = not normalized_target_type and bool(str(box_scan or "").strip())
    if legacy_pair:
        normalized_target_type = FbsInventorySession.SCOPE_BOX
    if normalized_target_type not in {
        FbsInventorySession.SCOPE_BOX,
        FbsInventorySession.SCOPE_CELL,
    }:
        raise FbsInventoryError("Выберите инвентаризацию короба или ячейки.")

    if normalized_target_type == FbsInventorySession.SCOPE_BOX:
        normalized_box_scan = str(target_scan or box_scan or "").strip()
        if not normalized_box_scan:
            raise FbsInventoryError("Отсканируйте QR короба FBS.")
        box = (
            FbsBox.objects.select_for_update()
            .select_related("agency", "pallet__cell__location")
            .exclude(status=FbsBox.STATUS_ARCHIVED)
            .filter(box_code__iexact=normalized_box_scan)
            .first()
        )
        if box is None:
            raise FbsInventoryError("Короб FBS не найден или уже находится в архиве.")
        assert_box_unlocked(box.id)
        session = create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_DRAIN,
            scan_mode=FbsInventorySession.SCAN_MODE_KIZ,
            agency=box.agency,
            box=box,
            created_by=actor,
            managed_workflow=True,
        )
        confirmed_place_scan = (
            str(place_scan or "").strip()
            if legacy_pair
            else box.pallet.cell.cell_code
        )
        confirmed_box_scan = normalized_box_scan
    else:
        normalized_place_scan = str(target_scan or place_scan or "").strip()
        if not normalized_place_scan:
            raise FbsInventoryError("Отсканируйте QR ячейки.")
        from .problems import _cell_scan_values, _normalized_cell_scan

        normalized_cell_scan = _normalized_cell_scan(normalized_place_scan)
        matching_cell_ids = [
            cell.id
            for cell in FbsStorageCell.objects.filter(is_active=True)
            .select_related("location")
            .order_by("id")
            if normalized_cell_scan in _cell_scan_values(cell)
        ]
        if not matching_cell_ids:
            raise FbsInventoryError("Ячейка FBS не найдена или выключена.")
        if len(matching_cell_ids) > 1:
            raise FbsInventoryError("QR соответствует нескольким ячейкам. Обратитесь к кладовщику.")
        cell = (
            FbsStorageCell.objects.select_for_update()
            .select_related("location")
            .get(pk=matching_cell_ids[0])
        )
        box_ids = list(
            FbsBox.objects.exclude(status=FbsBox.STATUS_ARCHIVED)
            .filter(pallet__cell=cell)
            .values_list("id", flat=True)
        )
        validate_boxes_unlocked(box_ids)
        session = create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_CELL,
            mode=FbsInventorySession.MODE_DRAIN,
            scan_mode=FbsInventorySession.SCAN_MODE_KIZ,
            cell=cell,
            created_by=actor,
            managed_workflow=True,
        )
        confirmed_place_scan = normalized_place_scan
        confirmed_box_scan = ""

    session.assigned_to = actor
    session.save(update_fields=["assigned_to", "updated_at"])
    event(
        session,
        "assigned",
        actor,
        round=1,
        user_id=actor.id,
        self_started=True,
        target_type=normalized_target_type,
    )
    return start_inventory_count(
        session_id=session.id,
        counted_by=actor,
        place_scan=confirmed_place_scan,
        box_scan=confirmed_box_scan,
    )


def assert_counter(session, user, *, started=True):
    second = session.status == "recount"
    expected_id = session.recount_assigned_to_id if second else session.assigned_to_id
    if not getattr(user, "is_authenticated", False) or not user.is_active or expected_id != user.id:
        raise FbsInventoryError("Пересчет не назначен этому сотруднику.")
    if not Employee.objects.filter(user=user, is_active=True, role__in=COUNTERS).exists():
        raise FbsInventoryError("Сотрудник больше не допущен к складскому пересчету.")
    if (
        second
        and session.first_counter_id == user.id
        and not same_counter_recount_allowed(session, user)
    ):
        raise FbsInventoryError("Повторный пересчет должен выполнить другой сотрудник.")
    if started and not (session.recount_started_at if second else session.count_started_at):
        raise FbsInventoryError("Сначала подтвердите адрес и начните пересчет.")


@transaction.atomic
def start_inventory_count(*, session_id, counted_by, place_scan, box_scan="", container_absent=False):
    from .inventory import _require_writes, activate_drained_inventory, _physical_scope_balances, assert_pallet_not_relocating
    _require_writes()
    session = FbsInventorySession.objects.select_for_update().get(pk=session_id)
    if session.status not in ("draining", "counting", "recount"):
        raise FbsInventoryError("Пересчет сейчас недоступен.")
    assert_counter(session, counted_by, started=False)
    if session.scope_type == "box":
        box = FbsBox.objects.select_for_update().select_related("pallet__cell__location").get(pk=session.box_id)
        cell = box.pallet.cell
        if str(box_scan).strip().casefold() != box.box_code.strip().casefold():
            raise FbsInventoryError("Отсканируйте указанный короб.")
    elif session.scope_type == "cell":
        cell = session.cell
    elif session.scope_type == "pallet":
        cell = session.pallet.cell
        if str(box_scan).strip().casefold() != session.pallet.pallet_code.strip().casefold():
            raise FbsInventoryError("Отсканируйте указанную паллету.")
    else:
        raise FbsInventoryError("Назначенный пересчет доступен для короба, паллеты или ячейки.")
    from .problems import _cell_scan_values, _normalized_cell_scan
    if session.scope_type != "box" and _normalized_cell_scan(place_scan) not in _cell_scan_values(cell):
        raise FbsInventoryError("Отсканируйте указанную ячейку.")
    # Another counting session must not move or adjust the same physical stock.
    for balance in _physical_scope_balances(session):
        assert_pallet_not_relocating(balance.box.pallet_id, agency_id=balance.agency_id)
        if FbsStorageLock.objects.filter(is_active=True, block_execution=True).exclude(session=session).filter(
            Q(scope_type="all") | Q(scope_type="agency", agency_id=balance.agency_id)
            | Q(scope_type="cell", cell_id=balance.box.pallet.cell_id)
            | Q(scope_type="pallet", pallet_id=balance.box.pallet_id)
            | Q(scope_type="box", box_id=balance.box_id)
            | Q(scope_type="sku", agency_id=balance.agency_id, sku_id=balance.sku_ref_id)
        ).exists():
            raise FbsInventoryError("Это место уже пересчитывается в другой инвентаризации.")
    if session.status == "draining":
        session = activate_drained_inventory(session_id=session.id)
    second = session.status == "recount"
    timestamp_field = "recount_started_at" if second else "count_started_at"
    if getattr(session, timestamp_field):
        return session
    counter_field = "second_counter" if second else "first_counter"
    setattr(session, timestamp_field, timezone.now())
    setattr(session, counter_field, counted_by)
    session.save(update_fields=[timestamp_field, counter_field, "updated_at"])
    event(session, "count_started", counted_by, round=2 if second else 1,
          place_scan=place_scan, container_scan=box_scan, container_absent=container_absent)
    return session


def prepare_inventory_approval(session, actor, final_counts):
    """Release only unpicked orders if a confirmed count cannot cover reserves.

    Called inside approve_inventory's transaction. Any unsafe state rolls back
    the entire approval including releases and audit events.
    """
    from .picking import release_order_reservation
    from .inventory import _active_scope_operations
    require_manager(actor, approve=True)
    if _active_scope_operations(session):
        raise FbsInventoryError("В области пересчета есть активные операции.")
    counts = {}
    locked_balances = {b.id: b for b in FbsStockBalance.objects.select_for_update().filter(
        id__in=session.lines.values("balance_id"),
    ).order_by("id")}
    for line in session.lines.select_related("balance"):
        if locked_balances[line.balance_id].qty != line.expected_qty:
            raise FbsInventoryError("Остаток изменился после начала пересчета. Нужна новая проверка.")
        if line.second_count_qty is not None and line.second_count_qty != line.first_count_qty:
            if line.id not in final_counts:
                raise FbsInventoryError("Два пересчета расходятся; укажите итог руководителя.")
            counts[line.balance_id] = int(final_counts[line.id])
        else:
            counts[line.balance_id] = int(line.first_count_qty or 0)
        if counts[line.balance_id] < 0:
            raise FbsInventoryError("Итог не может быть отрицательным.")
    affected = list(FbsOrderStockAllocation.objects.filter(
        balance_id__in=counts, status__in=("reserved", "picking"),
    ).select_related("balance", "pick_task").order_by("id"))
    order_ids = {a.order_item.order_id for a in affected if counts[a.balance_id] < a.balance.reserved_qty}
    for order_id in sorted(order_ids):
        # Never cancel/re-reserve an order that already has physical picked units.
        if FbsOrderStockAllocation.objects.filter(order_item__order_id=order_id, qty_picked__gt=0).exclude(
            status__in=("released", "canceled"),
        ).exists():
            raise FbsInventoryError(f"Заказ #{order_id} уже частично собран. Сначала завершите разбор его резерва.")
        release_order_reservation(order_id=order_id, released_by=actor)
        event(session, "reservation_released", actor, order_id=order_id)
    return order_ids


def retry_inventory_orders(session, actor, extra_order_ids=()):
    """Use the normal allocator; never resurrect an old canceled pick task."""
    from .picking import reserve_order_stock
    ids = set(session.pick_issues.values_list("task__order_id", flat=True)) | set(extra_order_ids)
    for order in FbsOrder.objects.filter(id__in=ids).order_by("id"):
        outcome = "requires_order_review"
        if order.internal_status in {
            FbsOrder.STATUS_AWAITING_STOCK, FbsOrder.STATUS_RESERVED, FbsOrder.STATUS_QUEUED_FOR_PICK,
        }:
            try:
                with transaction.atomic():
                    result = reserve_order_stock(order_id=order.id, reserved_by=actor)
                    outcome = "reserved" if result.reserved else "awaiting_stock"
            except FbsError as exc:
                event(session, "order_review_required", actor, order_id=order.id, reason=str(exc))
                continue
        # Physical checking is done, but an unfilled order's issue stays open.
        if outcome == "reserved":
            session.pick_issues.filter(task__order=order, status="open").update(
                status="resolved", resolved_by=actor, resolved_at=timezone.now(), updated_at=timezone.now(),
            )
        event(session, "order_followup", actor, order_id=order.id, outcome=outcome)
