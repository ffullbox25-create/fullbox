from __future__ import annotations

from collections import defaultdict
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from employees.models import Employee
from fbs.models import FbsStockBalance
from sklad.models import WarehouseLocation

from ..models import (
    WmsNewEvent,
    WmsNewInventoryLine,
    WmsNewInventoryLock,
    WmsNewInventoryScan,
    WmsNewInventorySession,
    WmsNewMovement,
    WmsNewOrder,
    WmsNewOrderItem,
    WmsNewProduct,
)


class InventoryOperationError(ValueError):
    pass


def _actor_user(actor):
    return actor if getattr(actor, "is_authenticated", False) else None


def _actor_name(actor) -> str:
    if actor is None:
        return ""
    full_name = getattr(actor, "get_full_name", lambda: "")()
    return full_name or getattr(actor, "username", "") or str(actor)


def _actor_employee(actor) -> Employee:
    user = _actor_user(actor)
    employee = (
        Employee.objects.filter(user=user, is_active=True).first()
        if user is not None
        else None
    )
    if employee is None:
        raise InventoryOperationError("Для пересчета нужен активный сотрудник Fullbox.")
    return employee


def _event(session: WmsNewInventorySession, action: str, *, actor=None, before=None, after=None):
    WmsNewEvent.objects.create(
        entity_type="inventory",
        entity_id=session.id,
        action=action,
        actor=_actor_user(actor),
        before=before or {},
        after=after or {},
        metadata={
            "number": session.number,
            "scope_type": session.scope_type,
            "mode": session.mode,
        },
    )


def _scope_balances(session: WmsNewInventorySession):
    queryset = FbsStockBalance.objects.select_related(
        "agency",
        "sku_ref",
        "box__pallet__cell__location",
    ).order_by(
        "box__pallet__cell__cell_code",
        "box__box_code",
        "sku_code",
        "id",
    )
    if session.scope_type == WmsNewInventorySession.SCOPE_AGENCY:
        queryset = queryset.filter(agency_id=session.agency_id)
    elif session.scope_type == WmsNewInventorySession.SCOPE_CELL:
        queryset = queryset.filter(box__pallet__cell_id=session.cell_id)
    elif session.scope_type == WmsNewInventorySession.SCOPE_PALLET:
        queryset = queryset.filter(box__pallet_id=session.pallet_id)
    elif session.scope_type == WmsNewInventorySession.SCOPE_BOX:
        queryset = queryset.filter(box_id=session.box_id)
    elif session.scope_type == WmsNewInventorySession.SCOPE_PRODUCT:
        if session.product.source_sku_id:
            queryset = queryset.filter(sku_ref_id=session.product.source_sku_id)
        else:
            queryset = queryset.filter(
                agency_id=session.product.agency_id,
                sku_code=session.product.article,
            )
    elif session.scope_type != WmsNewInventorySession.SCOPE_ALL:
        raise InventoryOperationError("Неизвестная область инвентаризации.")

    if session.scan_mode == WmsNewInventorySession.SCAN_MODE_KIZ:
        return queryset.exclude(marking_code="")
    if session.scan_mode == WmsNewInventorySession.SCAN_MODE_BARCODE:
        return queryset.filter(marking_code="").exclude(barcode="")
    raise InventoryOperationError("Неизвестный способ пересчета.")


def _product_for_balance(balance: FbsStockBalance):
    if balance.sku_ref_id:
        product = WmsNewProduct.objects.filter(source_sku_id=balance.sku_ref_id).first()
        if product:
            return product
    return WmsNewProduct.objects.filter(
        agency_id=balance.agency_id,
        article=balance.sku_code,
        is_archived=False,
    ).first()


def _materialize_lines(session: WmsNewInventorySession) -> None:
    if session.lines.exists():
        return
    balances = list(_scope_balances(session))
    if not balances:
        raise InventoryOperationError(
            "В выбранной области нет товара для указанного способа пересчета."
        )
    WmsNewInventoryLine.objects.bulk_create(
        [
            WmsNewInventoryLine(
                session=session,
                source_balance_id=balance.id,
                product=_product_for_balance(balance),
                agency=balance.agency,
                location_code=balance.box.pallet.cell.warehouse_location_code,
                cell_code=balance.box.pallet.cell.cell_code,
                pallet_code=balance.box.pallet.pallet_code,
                box_code=balance.box.box_code,
                sku_code=balance.sku_code,
                product_name=balance.name,
                barcode=balance.barcode,
                marking_code=balance.marking_code,
                expected_qty=int(balance.qty or 0),
                source_snapshot={
                    "identity_key": balance.identity_key,
                    "available_qty": int(balance.available_qty or 0),
                    "reserved_qty": int(balance.reserved_qty or 0),
                    "lot_code": balance.lot_code,
                    "expiry_date": (
                        balance.expiry_date.isoformat() if balance.expiry_date else None
                    ),
                    "source_updated_at": balance.updated_at.isoformat(),
                },
            )
            for balance in balances
        ]
    )


def _active_wms_new_operations(session: WmsNewInventorySession) -> bool:
    product_ids = list(
        {
            product_id
            for product_id in _scope_balances(session).values_list("sku_ref_id", flat=True)
            if product_id
        }
    )
    if not product_ids:
        return False
    return WmsNewOrderItem.objects.filter(
        sku_id__in=product_ids,
        order__status__in=(
            WmsNewOrder.STATUS_QUEUED,
            WmsNewOrder.STATUS_PICKING,
            WmsNewOrder.STATUS_PICKED,
        ),
    ).exists()


@transaction.atomic
def create_inventory(
    *,
    scope_type: str,
    mode: str,
    scan_mode: str,
    agency=None,
    cell=None,
    pallet=None,
    box=None,
    product=None,
    actor=None,
) -> WmsNewInventorySession:
    if scope_type not in dict(WmsNewInventorySession.SCOPE_CHOICES):
        raise InventoryOperationError("Неизвестная область инвентаризации.")
    if mode not in dict(WmsNewInventorySession.MODE_CHOICES):
        raise InventoryOperationError("Неизвестный режим инвентаризации.")
    if scan_mode not in dict(WmsNewInventorySession.SCAN_MODE_CHOICES):
        raise InventoryOperationError("Неизвестный способ пересчета.")
    if scope_type == WmsNewInventorySession.SCOPE_PRODUCT and product and not agency:
        agency = product.agency

    session = WmsNewInventorySession(
        number=f"INV-{timezone.now():%Y%m%d}-{uuid4().hex[:8].upper()}",
        scope_type=scope_type,
        mode=mode,
        scan_mode=scan_mode,
        agency=agency,
        cell=cell,
        pallet=pallet,
        box=box,
        product=product,
        created_by=_actor_user(actor),
        status=(
            WmsNewInventorySession.STATUS_DRAINING
            if mode == WmsNewInventorySession.MODE_DRAIN
            else WmsNewInventorySession.STATUS_COUNTING
        ),
        started_at=(None if mode == WmsNewInventorySession.MODE_DRAIN else timezone.now()),
        pilot_revision=1,
    )
    session.full_clean()
    if not _scope_balances(session).exists():
        raise InventoryOperationError(
            "В выбранной области нет товара для указанного способа пересчета."
        )
    session.save()
    if mode != WmsNewInventorySession.MODE_AUDIT:
        WmsNewInventoryLock.objects.create(
            session=session,
            block_new_operations=True,
            block_execution=mode == WmsNewInventorySession.MODE_IMMEDIATE,
        )
    if session.status == WmsNewInventorySession.STATUS_COUNTING:
        _materialize_lines(session)
    _event(
        session,
        "inventory_created",
        actor=actor,
        after={"status": session.status, "lines": session.lines.count()},
    )
    return session


@transaction.atomic
def activate_inventory(*, session_id: int, actor=None) -> WmsNewInventorySession:
    session = WmsNewInventorySession.objects.select_for_update().get(pk=session_id)
    if session.status == WmsNewInventorySession.STATUS_COUNTING:
        return session
    if session.status != WmsNewInventorySession.STATUS_DRAINING:
        raise InventoryOperationError("Инвентаризация не ожидает завершения операций.")
    if _active_wms_new_operations(session):
        raise InventoryOperationError(
            "В выбранной области еще есть активные операции WMS NEW."
        )
    lock = WmsNewInventoryLock.objects.select_for_update().filter(
        session=session,
        is_active=True,
    ).first()
    if lock:
        lock.block_execution = True
        lock.save(update_fields=("block_execution", "updated_at"))
    before = {"status": session.status}
    session.status = WmsNewInventorySession.STATUS_COUNTING
    session.started_at = timezone.now()
    session.pilot_revision += 1
    session.save(update_fields=("status", "started_at", "pilot_revision", "updated_at"))
    _materialize_lines(session)
    _event(
        session,
        "inventory_activated",
        actor=actor,
        before=before,
        after={"status": session.status, "lines": session.lines.count()},
    )
    return session


def _scan_line(session: WmsNewInventorySession, scan_code: str) -> WmsNewInventoryLine:
    field = (
        "marking_code__iexact"
        if session.scan_mode == WmsNewInventorySession.SCAN_MODE_KIZ
        else "barcode__iexact"
    )
    matches = list(
        WmsNewInventoryLine.objects.select_for_update().filter(
            session=session,
            **{field: scan_code},
        )
    )
    if not matches:
        label = "КИЗ" if session.scan_mode == WmsNewInventorySession.SCAN_MODE_KIZ else "штрихкод"
        raise InventoryOperationError(f"{label} не найден в выбранной области.")
    if len(matches) > 1:
        raise InventoryOperationError(
            "Код относится к нескольким строкам. Выберите более точную область инвентаризации."
        )
    return matches[0]


@transaction.atomic
def record_inventory_scan(
    *,
    session_id: int,
    scan_code: str,
    actor,
) -> WmsNewInventoryLine:
    employee = _actor_employee(actor)
    scan_code = str(scan_code or "").strip()
    if not scan_code:
        raise InventoryOperationError("Отсканируйте КИЗ или штрихкод.")
    session = WmsNewInventorySession.objects.select_for_update().get(pk=session_id)
    if session.status not in {
        WmsNewInventorySession.STATUS_COUNTING,
        WmsNewInventorySession.STATUS_RECOUNT,
    }:
        raise InventoryOperationError("Инвентаризация сейчас не принимает сканы.")
    first_round = session.status == WmsNewInventorySession.STATUS_COUNTING
    count_round = (
        WmsNewInventoryScan.ROUND_FIRST
        if first_round
        else WmsNewInventoryScan.ROUND_SECOND
    )
    if first_round:
        if session.first_counter_id not in {None, employee.id}:
            raise InventoryOperationError("Первый пересчет уже выполняет другой сотрудник.")
        if session.first_counter_id is None:
            session.first_counter = employee
            session.save(update_fields=("first_counter", "updated_at"))
    else:
        if session.first_counter_id == employee.id:
            raise InventoryOperationError("Повторный пересчет должен выполнить другой сотрудник.")
        if session.second_counter_id not in {None, employee.id}:
            raise InventoryOperationError("Повторный пересчет уже выполняет другой сотрудник.")
        if session.second_counter_id is None:
            session.second_counter = employee
            session.save(update_fields=("second_counter", "updated_at"))

    line = _scan_line(session, scan_code)
    if session.scan_mode == WmsNewInventorySession.SCAN_MODE_KIZ and WmsNewInventoryScan.objects.filter(
        line__session=session,
        count_round=count_round,
        scan_code__iexact=scan_code,
    ).exists():
        raise InventoryOperationError("Этот КИЗ уже отсканирован в текущем пересчете.")
    WmsNewInventoryScan.objects.create(
        line=line,
        count_round=count_round,
        scan_code=scan_code,
        counted_by=employee,
    )
    field = "first_count_qty" if first_round else "second_count_qty"
    setattr(line, field, int(getattr(line, field) or 0) + 1)
    line.save(update_fields=(field, "updated_at"))
    session.pilot_revision += 1
    session.save(update_fields=("pilot_revision", "updated_at"))
    _event(
        session,
        "inventory_scan",
        actor=actor,
        after={"line_id": line.id, "round": count_round, "scan_code": scan_code},
    )
    return line


@transaction.atomic
def finish_inventory_count(*, session_id: int, actor) -> WmsNewInventorySession:
    employee = _actor_employee(actor)
    session = WmsNewInventorySession.objects.select_for_update().get(pk=session_id)
    lines = list(WmsNewInventoryLine.objects.select_for_update().filter(session=session))
    before = {"status": session.status}
    if session.status == WmsNewInventorySession.STATUS_COUNTING:
        if session.first_counter_id != employee.id:
            raise InventoryOperationError("Первый пересчет может завершить только его исполнитель.")
        for line in lines:
            if line.first_count_qty is None:
                line.first_count_qty = 0
        WmsNewInventoryLine.objects.bulk_update(lines, ("first_count_qty", "updated_at"))
        session.status = (
            WmsNewInventorySession.STATUS_RECOUNT
            if any(line.first_count_qty != line.expected_qty for line in lines)
            else WmsNewInventorySession.STATUS_APPROVAL
        )
    elif session.status == WmsNewInventorySession.STATUS_RECOUNT:
        if session.second_counter_id != employee.id:
            raise InventoryOperationError("Повторный пересчет может завершить только его исполнитель.")
        for line in lines:
            if line.second_count_qty is None:
                line.second_count_qty = 0
        WmsNewInventoryLine.objects.bulk_update(lines, ("second_count_qty", "updated_at"))
        session.status = WmsNewInventorySession.STATUS_APPROVAL
    else:
        raise InventoryOperationError("Пересчет нельзя завершить в текущем статусе.")
    session.pilot_revision += 1
    session.save(update_fields=("status", "pilot_revision", "updated_at"))
    _event(
        session,
        "inventory_count_finished",
        actor=actor,
        before=before,
        after={"status": session.status},
    )
    return session


@transaction.atomic
def approve_inventory(
    *,
    session_id: int,
    actor,
    final_counts: dict[int, int] | None = None,
) -> WmsNewInventorySession:
    user = _actor_user(actor)
    if user is None:
        raise InventoryOperationError("Инвентаризацию должен утвердить руководитель.")
    session = WmsNewInventorySession.objects.select_for_update().get(pk=session_id)
    if session.status != WmsNewInventorySession.STATUS_APPROVAL:
        raise InventoryOperationError("Инвентаризация еще не готова к утверждению.")
    final_counts = final_counts or {}
    lines = list(
        WmsNewInventoryLine.objects.select_for_update()
        .select_related("product")
        .filter(session=session)
    )
    product_deltas = defaultdict(int)
    for line in lines:
        first = int(line.first_count_qty or 0)
        second = line.second_count_qty
        if second is None or int(second) == first:
            final_qty = first
        elif line.id in final_counts:
            try:
                final_qty = int(final_counts[line.id])
            except (TypeError, ValueError):
                raise InventoryOperationError("Итоговое количество должно быть целым числом.")
        else:
            raise InventoryOperationError(
                f"По строке {line.id} пересчеты расходятся; укажите итог руководителя."
            )
        if final_qty < 0:
            raise InventoryOperationError("Итоговый остаток не может быть отрицательным.")
        reserved_qty = int((line.source_snapshot or {}).get("reserved_qty") or 0)
        if final_qty < reserved_qty:
            raise InventoryOperationError(
                f"По строке {line.id} итог меньше зарезервированного количества."
            )
        line.final_qty = final_qty
        line.approved_delta = final_qty - int(line.expected_qty or 0)
        if line.product_id:
            product_deltas[line.product_id] += line.approved_delta

    if session.mode != WmsNewInventorySession.MODE_AUDIT:
        products = {
            item.id: item
            for item in WmsNewProduct.objects.select_for_update().filter(
                id__in=product_deltas
            )
        }
        for product_id, delta in product_deltas.items():
            product = products[product_id]
            new_on_hand = int(product.stock_on_hand or 0) + delta
            reserved = (
                int(product.fbo_reserved or 0)
                + int(product.fbs_reserved or 0)
                + int(product.internal_reserved or 0)
            )
            if new_on_hand < reserved:
                raise InventoryOperationError(
                    f"Итог по товару {product.article} меньше действующего резерва."
                )
            product.stock_on_hand = new_on_hand
            product.stock_free = min(
                max(new_on_hand - reserved, 0),
                max(int(product.stock_free or 0) + delta, 0),
            )
            product.pilot_revision += 1
            product.save(
                update_fields=(
                    "stock_on_hand",
                    "stock_free",
                    "pilot_revision",
                    "updated_at",
                )
            )
            WmsNewEvent.objects.create(
                entity_type="product",
                entity_id=product.id,
                action="inventory_adjusted",
                actor=user,
                before={"delta": 0},
                after={"delta": delta, "stock_on_hand": new_on_hand},
                metadata={"inventory_id": session.id, "inventory_number": session.number},
            )
        location_by_code = {
            item.location_code: item
            for item in WarehouseLocation.objects.filter(
                location_code__in={line.location_code for line in lines if line.location_code}
            )
        }
        for line in lines:
            if not line.product_id or not line.approved_delta:
                continue
            product = products[line.product_id]
            location = location_by_code.get(line.location_code)
            WmsNewMovement.objects.create(
                agency_id=line.agency_id,
                product=product,
                product_name=line.product_name,
                article=line.sku_code,
                action=WmsNewMovement.ACTION_ADJUSTMENT,
                source_location=location,
                source_location_name=line.location_code,
                target_location=location,
                target_location_name=line.location_code,
                quantity=line.approved_delta,
                balance_after=product.stock_on_hand,
                boxes_count=1 if line.box_code else 0,
                information=f"Инвентаризация {session.number}: {line.box_code or line.cell_code}",
                source_event_type="wms_new_inventory",
                actor=user,
                actor_name=_actor_name(user),
                occurred_at=timezone.now(),
                payload={
                    "inventory_id": session.id,
                    "line_id": line.id,
                    "cell_code": line.cell_code,
                    "pallet_code": line.pallet_code,
                    "box_code": line.box_code,
                    "expected_qty": line.expected_qty,
                    "final_qty": line.final_qty,
                },
                is_manual=True,
            )

    WmsNewInventoryLine.objects.bulk_update(
        lines,
        ("final_qty", "approved_delta", "updated_at"),
    )
    now = timezone.now()
    WmsNewInventoryLock.objects.filter(session=session, is_active=True).update(
        is_active=False,
        released_at=now,
        updated_at=now,
    )
    before = {"status": session.status}
    session.status = WmsNewInventorySession.STATUS_DONE
    session.approved_by = user
    session.completed_at = now
    session.pilot_revision += 1
    session.save(
        update_fields=(
            "status",
            "approved_by",
            "completed_at",
            "pilot_revision",
            "updated_at",
        )
    )
    _event(
        session,
        "inventory_approved",
        actor=actor,
        before=before,
        after={
            "status": session.status,
            "delta": sum(line.approved_delta for line in lines),
        },
    )
    return session


@transaction.atomic
def cancel_inventory(*, session_id: int, actor=None) -> WmsNewInventorySession:
    session = WmsNewInventorySession.objects.select_for_update().get(pk=session_id)
    if session.status == WmsNewInventorySession.STATUS_DONE:
        raise InventoryOperationError("Завершенную инвентаризацию нельзя отменить.")
    if session.status == WmsNewInventorySession.STATUS_CANCELLED:
        return session
    now = timezone.now()
    WmsNewInventoryLock.objects.filter(session=session, is_active=True).update(
        is_active=False,
        released_at=now,
        updated_at=now,
    )
    before = {"status": session.status}
    session.status = WmsNewInventorySession.STATUS_CANCELLED
    session.completed_at = now
    session.pilot_revision += 1
    session.save(
        update_fields=("status", "completed_at", "pilot_revision", "updated_at")
    )
    _event(
        session,
        "inventory_cancelled",
        actor=actor,
        before=before,
        after={"status": session.status},
    )
    return session


def assert_orders_unlocked(orders, *, for_execution: bool = False) -> None:
    """Apply inventory locks to WMS NEW orders without touching legacy FBS locks."""

    orders = list(orders)
    if not orders:
        return
    flag = "block_execution" if for_execution else "block_new_operations"
    sessions = list(
        WmsNewInventorySession.objects.filter(
            storage_lock__is_active=True,
            **{f"storage_lock__{flag}": True},
        ).select_related("product").prefetch_related("lines")
    )
    if not sessions:
        return

    order_ids = [order.id for order in orders]
    sku_ids_by_order = defaultdict(set)
    for order_id, sku_id in WmsNewOrderItem.objects.filter(
        order_id__in=order_ids,
        sku_id__isnull=False,
    ).values_list("order_id", "sku_id"):
        sku_ids_by_order[order_id].add(sku_id)

    for session in sessions:
        if session.scope_type == WmsNewInventorySession.SCOPE_ALL:
            raise InventoryOperationError(
                f"Инвентаризация {session.number} блокирует операции WMS NEW по складу."
            )
        if session.scope_type == WmsNewInventorySession.SCOPE_AGENCY:
            if any(order.agency_id == session.agency_id for order in orders):
                raise InventoryOperationError(
                    f"Инвентаризация {session.number} блокирует операции партнера в WMS NEW."
                )
            continue

        if session.scope_type == WmsNewInventorySession.SCOPE_PRODUCT:
            locked_sku_ids = set()
            if session.product_id and session.product.source_sku_id:
                locked_sku_ids.add(session.product.source_sku_id)
        else:
            locked_sku_ids = set(
                session.lines.exclude(product__source_sku_id__isnull=True).values_list(
                    "product__source_sku_id",
                    flat=True,
                )
            )
            if not locked_sku_ids:
                locked_sku_ids = {
                    sku_id
                    for sku_id in _scope_balances(session).values_list(
                        "sku_ref_id",
                        flat=True,
                    )
                    if sku_id
                }
        if any(sku_ids_by_order[order.id] & locked_sku_ids for order in orders):
            raise InventoryOperationError(
                f"Инвентаризация {session.number} блокирует операции с товаром в WMS NEW."
            )
