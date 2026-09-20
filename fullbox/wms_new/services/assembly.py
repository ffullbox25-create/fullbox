from __future__ import annotations

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from fbs.models import FbsPickingCart, FbsWorkstation

from wms_new.models import (
    WmsNewAssemblyLine,
    WmsNewAssemblySession,
    WmsNewEvent,
    WmsNewOrder,
    WmsNewProduct,
    WmsNewWave,
    WmsNewWavePickLine,
)


class AssemblyOperationError(ValueError):
    pass


def _event(entity_type, entity_id, action, actor, before=None, after=None):
    WmsNewEvent.objects.create(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor=actor,
        before=before or {},
        after=after or {},
    )


def resolve_assembly_place(value: str) -> tuple[str, str]:
    scan = str(value or "").strip()
    if not scan:
        raise AssemblyOperationError("Отсканируйте ID места с товаром.")
    workstation = FbsWorkstation.objects.filter(is_active=True).filter(
        Q(barcode__iexact=scan) | Q(name__iexact=scan)
    ).first()
    if workstation:
        return workstation.barcode, workstation.name
    cart = FbsPickingCart.objects.filter(is_active=True).filter(
        Q(barcode__iexact=scan) | Q(name__iexact=scan)
    ).first()
    if cart:
        return cart.barcode, cart.name
    return scan[:128], scan[:128]


def _source_picked_order_ids(wave: WmsNewWave) -> set[int] | None:
    tasks = (wave.source_snapshot or {}).get("tasks")
    if not isinstance(tasks, list):
        return None
    return {
        int(task["source_order_id"])
        for task in tasks
        if str(task.get("status") or "") == "picked" and task.get("source_order_id")
    }


@transaction.atomic
def start_assembly_session(*, place_scan: str, actor=None) -> WmsNewAssemblySession:
    place_code, place_name = resolve_assembly_place(place_scan)
    existing = (
        WmsNewAssemblySession.objects.select_for_update()
        .filter(status=WmsNewAssemblySession.STATUS_ACTIVE, place_name__iexact=place_name)
        .first()
    )
    if existing:
        return existing

    waves = list(
        WmsNewWave.objects.select_for_update()
        .filter(status=WmsNewWave.STATUS_VERIFICATION)
        .filter(Q(cart_name__iexact=place_name) | Q(workstation_name__iexact=place_name))
        .prefetch_related("wave_orders__order__items", "pick_lines__order", "pick_lines__order_item")
        .order_by("id")
    )
    if not waves:
        raise AssemblyOperationError(
            "В этом месте нет волн FBS-NEW, готовых к сборке заказов."
        )

    session = WmsNewAssemblySession.objects.create(
        place_code=place_code,
        place_name=place_name,
        started_by=actor,
        source_snapshot={"wave_ids": [wave.id for wave in waves]},
        pilot_revision=1,
    )
    planned_items = 0
    created_lines = 0
    for wave in waves:
        wave_pick_lines = list(wave.pick_lines.select_related("order", "order_item").all())
        if wave_pick_lines:
            for pick_line in wave_pick_lines:
                if pick_line.picked_quantity <= 0:
                    continue
                order = pick_line.order
                if order.status in {
                    WmsNewOrder.STATUS_CANCELLED,
                    WmsNewOrder.STATUS_HANDED_OVER,
                    WmsNewOrder.STATUS_DONE,
                    WmsNewOrder.STATUS_RETURNED,
                }:
                    continue
                quantity = int(pick_line.picked_quantity)
                WmsNewAssemblyLine.objects.create(
                    session=session,
                    wave=wave,
                    order=order,
                    order_item=pick_line.order_item,
                    planned_quantity=quantity,
                    requires_order_scan=True,
                )
                planned_items += quantity
                created_lines += 1
            continue
        picked_source_ids = _source_picked_order_ids(wave)
        for link in wave.wave_orders.all():
            order = link.order
            if picked_source_ids is not None and order.source_order_id not in picked_source_ids:
                continue
            if order.status in {
                WmsNewOrder.STATUS_CANCELLED,
                WmsNewOrder.STATUS_HANDED_OVER,
                WmsNewOrder.STATUS_DONE,
                WmsNewOrder.STATUS_RETURNED,
            }:
                continue
            order_items = list(order.items.all())
            for item in order_items:
                quantity = max(1, int(item.quantity or 1))
                WmsNewAssemblyLine.objects.create(
                    session=session,
                    wave=wave,
                    order=order,
                    order_item=item,
                    planned_quantity=quantity,
                    requires_order_scan=True,
                )
                planned_items += quantity
                created_lines += 1
    if not created_lines:
        raise AssemblyOperationError(
            "В этом месте нет отобранных товаров, доступных для сборки FBS-NEW."
        )
    session.planned_items = planned_items
    session.save(update_fields=("planned_items", "updated_at"))
    _event(
        "assembly_session",
        session.id,
        "start",
        actor,
        after={"place": place_name, "planned_items": planned_items},
    )
    return session


def _required_extra_fields(line: WmsNewAssemblyLine) -> tuple[str, ...]:
    requirements = line.order_item.requirements or {}
    aliases = {
        "marking_code": ("need_marking", "need-marking", "marking_required"),
        "imei": ("need_imei", "need-imei"),
        "uin": ("need_uin", "need-uin"),
        "gtin": ("need_gtin", "need-gtin"),
        "sgtin": ("need_sgtin", "need-sgtin"),
        "expiration_date": ("need_expiration", "need-expiration", "expiration_required"),
    }
    return tuple(
        field
        for field, keys in aliases.items()
        if any(requirements.get(key) in (True, 1, "1", "yes") for key in keys)
    )


def _validate_extra_data(line: WmsNewAssemblyLine, extra_data: dict | None) -> dict:
    values = {
        str(key): str(value).strip()
        for key, value in (extra_data or {}).items()
        if str(value).strip()
    }
    missing = [field for field in _required_extra_fields(line) if not values.get(field)]
    if missing:
        raise AssemblyOperationError(
            "Для товара обязательны дополнительные данные: " + ", ".join(missing)
        )
    return values


def _mark_order_ready_if_complete(line: WmsNewAssemblyLine, actor=None):
    order = line.order
    remaining = order.assembly_lines.exclude(
        status=WmsNewAssemblyLine.STATUS_ASSEMBLED
    ).exclude(session__status=WmsNewAssemblySession.STATUS_CANCELLED)
    if not remaining.exists():
        before = {"status": order.status, "pilot_revision": order.pilot_revision}
        order.status = WmsNewOrder.STATUS_READY
        order.pilot_revision += 1
        order.save(update_fields=("status", "pilot_revision", "updated_at"))
        _event("order", order.id, "assembly_ready", actor, before, {"status": order.status})


def _finish_wave_if_complete(wave: WmsNewWave, actor=None):
    active_lines = wave.assembly_lines.exclude(
        session__status=WmsNewAssemblySession.STATUS_CANCELLED
    )
    if not active_lines.exists() or active_lines.filter(
        status__in=(WmsNewAssemblyLine.STATUS_PENDING, WmsNewAssemblyLine.STATUS_AWAITING_ORDER)
    ).exists():
        return
    if wave.status == WmsNewWave.STATUS_DONE:
        return
    before = {"status": wave.status, "picked_units": wave.picked_units}
    wave.status = WmsNewWave.STATUS_DONE
    wave.completed_at = timezone.now()
    snapshot = dict(wave.source_snapshot or {})
    snapshot["has_problems"] = active_lines.filter(
        status=WmsNewAssemblyLine.STATUS_PROBLEM
    ).exists()
    wave.source_snapshot = snapshot
    wave.pilot_revision += 1
    wave.save()
    _event(
        "wave",
        wave.id,
        "assembly_complete",
        actor,
        before,
        {"status": wave.status, "picked_units": wave.picked_units},
    )


def _refresh_session(session: WmsNewAssemblySession, actor=None):
    values = session.lines.aggregate(
        assembled=Sum("assembled_quantity"),
    )
    session.assembled_items = int(values["assembled"] or 0)
    session.problem_items = sum(
        line.planned_quantity
        for line in session.lines.filter(status=WmsNewAssemblyLine.STATUS_PROBLEM)
    )
    session.current_order = None
    unfinished = session.lines.filter(
        status__in=(
            WmsNewAssemblyLine.STATUS_PENDING,
            WmsNewAssemblyLine.STATUS_AWAITING_ORDER,
        )
    ).exists()
    if not unfinished:
        session.status = WmsNewAssemblySession.STATUS_COMPLETED
        session.completed_by = actor
        session.completed_at = timezone.now()
    session.pilot_revision += 1
    session.save()
    if session.status == WmsNewAssemblySession.STATUS_COMPLETED:
        _event(
            "assembly_session",
            session.id,
            "complete",
            actor,
            after={
                "assembled_items": session.assembled_items,
                "problem_items": session.problem_items,
            },
        )


def _assemble_one(line: WmsNewAssemblyLine, *, actor=None, order_scan: str = ""):
    line.assembled_quantity = min(line.planned_quantity, line.assembled_quantity + 1)
    line.order_scan = order_scan
    line.assembled_by = actor
    line.assembled_at = timezone.now()
    line.status = (
        WmsNewAssemblyLine.STATUS_ASSEMBLED
        if line.assembled_quantity >= line.planned_quantity
        else WmsNewAssemblyLine.STATUS_PENDING
    )
    line.save()
    product = None
    if line.order_item.sku_id:
        product = WmsNewProduct.objects.select_for_update().filter(
            source_sku_id=line.order_item.sku_id
        ).first()
    if product is None and line.order_item.barcode:
        product = WmsNewProduct.objects.select_for_update().filter(
            agency_id=line.order.agency_id,
            barcode__iexact=line.order_item.barcode,
        ).first()
    if product is not None:
        product.stock_on_hand = max(0, product.stock_on_hand - 1)
        product.internal_reserved = max(0, product.internal_reserved - 1)
        product.pilot_revision += 1
        product.save(
            update_fields=("stock_on_hand", "internal_reserved", "pilot_revision", "updated_at")
        )
    _event(
        "assembly_line",
        line.id,
        "assemble_good",
        actor,
        after={
            "order_id": line.order_id,
            "assembled_quantity": line.assembled_quantity,
            "planned_quantity": line.planned_quantity,
        },
    )
    if line.status == WmsNewAssemblyLine.STATUS_ASSEMBLED:
        _mark_order_ready_if_complete(line, actor=actor)
        _finish_wave_if_complete(line.wave, actor=actor)
    _refresh_session(line.session, actor=actor)


@transaction.atomic
def scan_assembly_product(
    *, session_id: int, barcode: str, extra_data: dict | None = None, actor=None
) -> WmsNewAssemblyLine:
    scan = str(barcode or "").strip()
    if not scan:
        raise AssemblyOperationError("Отсканируйте ШК товара.")
    session = WmsNewAssemblySession.objects.select_for_update().get(pk=session_id)
    if session.status != WmsNewAssemblySession.STATUS_ACTIVE:
        raise AssemblyOperationError("Эта сборка уже завершена.")
    candidates = list(
        WmsNewAssemblyLine.objects.select_for_update()
        .select_related("order", "order_item", "wave", "session")
        .filter(session=session, status=WmsNewAssemblyLine.STATUS_PENDING)
        .filter(Q(order_item__barcode__iexact=scan) | Q(order_item__external_sku__iexact=scan))
        .order_by("id")
    )
    if not candidates:
        raise AssemblyOperationError(
            "Товар не найден в месте сборки. Отсканируйте повторно или отложите проблемный товар."
        )
    line = candidates[0]
    values = _validate_extra_data(line, extra_data)
    line.product_scan = scan
    line.extra_data = values
    if line.requires_order_scan:
        if not line.order.tracking_number:
            raise AssemblyOperationError(
                "У заказа еще нет трек-номера маркетплейса. Отложите товар или повторите позже."
            )
        line.status = WmsNewAssemblyLine.STATUS_AWAITING_ORDER
        line.save(update_fields=("product_scan", "extra_data", "status", "updated_at"))
        session.current_order = line.order
        session.pilot_revision += 1
        session.save(update_fields=("current_order", "pilot_revision", "updated_at"))
        _event(
            "assembly_line",
            line.id,
            "product_scan",
            actor,
            after={"barcode": scan, "awaiting_order": True},
        )
    else:
        line.save(update_fields=("product_scan", "extra_data", "updated_at"))
        _assemble_one(line, actor=actor)
    return line


@transaction.atomic
def scan_assembly_order_label(*, session_id: int, barcode: str, actor=None) -> WmsNewAssemblyLine:
    scan = str(barcode or "").strip()
    session = WmsNewAssemblySession.objects.select_for_update().get(pk=session_id)
    line = (
        WmsNewAssemblyLine.objects.select_for_update()
        .select_related("order", "order_item", "wave", "session")
        .filter(session=session, status=WmsNewAssemblyLine.STATUS_AWAITING_ORDER)
        .order_by("updated_at", "id")
        .first()
    )
    if line is None:
        raise AssemblyOperationError("Сначала отсканируйте товар.")
    expected = str(line.order.tracking_number or "").strip()
    marketplace = str(line.order.marketplace or "").lower()
    valid = scan.startswith(expected) if "yandex" in marketplace else scan == expected
    if not expected or not valid:
        raise AssemblyOperationError("ШК заказа не совпадает с заказом выбранного товара.")
    _assemble_one(line, actor=actor, order_scan=scan)
    return line


@transaction.atomic
def move_assembly_problem(
    *, session_id: int, problem_place: str, reason: str = "", line_id: int | None = None, actor=None
) -> WmsNewAssemblyLine:
    place = str(problem_place or "").strip()
    if not place:
        raise AssemblyOperationError("Отсканируйте место для проблемного товара.")
    session = WmsNewAssemblySession.objects.select_for_update().get(pk=session_id)
    lines = WmsNewAssemblyLine.objects.select_for_update().select_related(
        "order", "order_item", "wave", "session"
    ).filter(session=session)
    line = lines.filter(pk=line_id).first() if line_id else lines.filter(
        status=WmsNewAssemblyLine.STATUS_AWAITING_ORDER
    ).order_by("updated_at", "id").first()
    if line is None:
        raise AssemblyOperationError("Сначала отсканируйте проблемный товар.")
    problem_lines = list(lines.filter(order_id=line.order_id))
    for problem_line in problem_lines:
        problem_line.status = WmsNewAssemblyLine.STATUS_PROBLEM
        problem_line.problem_place = place[:128]
        problem_line.problem_reason = str(reason or "").strip()
        problem_line.assembled_by = actor
        problem_line.save()
    line.refresh_from_db()
    order = line.order
    before = {"status": order.status, "pilot_revision": order.pilot_revision}
    order.status = WmsNewOrder.STATUS_EXCEPTION
    order.pilot_revision += 1
    order.save(update_fields=("status", "pilot_revision", "updated_at"))
    _event(
        "assembly_line",
        line.id,
        "problem_good",
        actor,
        after={
            "problem_place": line.problem_place,
            "reason": line.problem_reason,
            "order_line_ids": [problem_line.id for problem_line in problem_lines],
        },
    )
    _finish_wave_if_complete(line.wave, actor=actor)
    _event("order", order.id, "assembly_problem", actor, before, {"status": order.status})
    _refresh_session(session, actor=actor)
    return line
