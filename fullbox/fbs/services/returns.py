from __future__ import annotations

import hashlib
import logging

from PIL import Image, UnidentifiedImageError
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from fbs.exceptions import FbsFeatureDisabled, FbsReturnError
from fbs.flags import feature_enabled
from fbs.models import (
    FbsBox,
    FbsOrder,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsReturn,
    FbsReturnPhoto,
    FbsReturnUnit,
    FbsStockBalance,
)
from sklad.models import WarehouseEvent


logger = logging.getLogger(__name__)
MAX_RETURN_PHOTO_BYTES = 10 * 1024 * 1024
ELIGIBLE_ORDER_STATUSES = {
    FbsOrder.STATUS_HANDED_OVER,
    FbsOrder.STATUS_DELIVERED,
    FbsOrder.STATUS_RETURN_PENDING,
    FbsOrder.STATUS_RETURNED,
}
ACTIVE_RETURN_STATUSES = {
    FbsReturn.STATUS_EXPECTED,
    FbsReturn.STATUS_RECEIVING,
    FbsReturn.STATUS_INSPECTION,
}


def _normalized(value) -> str:
    return str(value or "").strip()


def _require_writes() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Складские операции FBS выключены.")


def _actor(user):
    if not getattr(user, "is_authenticated", False):
        raise FbsReturnError("Для операции возврата нужен авторизованный сотрудник.")
    return user


def _picked_allocations(order_id: int, *, for_update: bool = False):
    queryset = FbsOrderStockAllocation.objects.filter(
        order_item__order_id=order_id,
        status=FbsOrderStockAllocation.STATUS_PICKED,
        qty_picked__gt=0,
    ).select_related("order_item__sku", "balance", "traceability")
    if for_update:
        queryset = queryset.select_for_update(of=("self",))
    return queryset.order_by("order_item_id", "id")


def remaining_returnable_qty(order_id: int) -> int:
    picked = int(
        _picked_allocations(order_id).aggregate(total=Sum("qty_picked"))["total"] or 0
    )
    returned = FbsReturnUnit.objects.filter(
        return_record__order_id=order_id,
    ).count()
    return max(picked - returned, 0)


@transaction.atomic
def register_fbs_return(
    *,
    order_id: int,
    expected_qty: int,
    created_by,
    external_return_id: str = "",
    reason: str = "",
) -> FbsReturn:
    _require_writes()
    actor = _actor(created_by)
    order = (
        FbsOrder.objects.select_for_update()
        .select_related("profile__agency")
        .get(pk=order_id)
    )
    active = (
        FbsReturn.objects.select_for_update()
        .filter(order=order, status__in=ACTIVE_RETURN_STATUSES)
        .order_by("id")
        .first()
    )
    if active is not None:
        return active
    if order.internal_status not in ELIGIBLE_ORDER_STATUSES:
        raise FbsReturnError(
            "Возврат можно зарегистрировать только после передачи или доставки FBS-заказа."
        )
    allocations = list(_picked_allocations(order.id, for_update=True))
    if not allocations:
        raise FbsReturnError("У заказа нет подтвержденного FBS-отбора для возврата.")
    remaining = remaining_returnable_qty(order.id)
    try:
        expected_qty = int(expected_qty)
    except (TypeError, ValueError):
        expected_qty = 0
    if expected_qty <= 0:
        raise FbsReturnError("Количество возврата должно быть больше нуля.")
    if expected_qty > remaining:
        raise FbsReturnError(
            f"Можно принять не более {remaining} шт. по подтвержденному FBS-отбору."
        )
    external_return_id = _normalized(external_return_id)
    if external_return_id and FbsReturn.objects.filter(
        order=order,
        external_return_id=external_return_id,
    ).exists():
        raise FbsReturnError("Возврат с таким номером маркетплейса уже зарегистрирован.")
    return_record = FbsReturn.objects.create(
        order=order,
        external_return_id=external_return_id,
        expected_qty=expected_qty,
        reason=_normalized(reason),
        created_by=actor,
    )
    order.internal_status = FbsOrder.STATUS_RETURN_PENDING
    order.save(update_fields=["internal_status", "updated_at"])
    return return_record


def _valid_order_scans(order: FbsOrder) -> set[str]:
    values = {_normalized(order.external_order_id)}
    for label in order.marketplace_labels.all():
        values.add(_normalized(label.barcode))
        values.add(_normalized(label.external_label_id))
    return {value for value in values if value}


@transaction.atomic
def confirm_return_order_scan(
    *, return_id: int, order_scan: str, performed_by
) -> FbsReturn:
    _require_writes()
    actor = _actor(performed_by)
    return_record = (
        FbsReturn.objects.select_for_update()
        .select_related("order")
        .prefetch_related("order__marketplace_labels")
        .get(pk=return_id)
    )
    if return_record.status == FbsReturn.STATUS_COMPLETED:
        return return_record
    order_scan = _normalized(order_scan)
    if not order_scan or order_scan not in _valid_order_scans(return_record.order):
        raise FbsReturnError("QR заказа не совпадает с зарегистрированным возвратом.")
    now = timezone.now()
    return_record.order_scan_value = order_scan
    return_record.order_scan_confirmed_at = now
    return_record.received_by = actor
    return_record.received_at = return_record.received_at or now
    return_record.status = FbsReturn.STATUS_RECEIVING
    return_record.save(
        update_fields=[
            "order_scan_value",
            "order_scan_confirmed_at",
            "received_by",
            "received_at",
            "status",
            "updated_at",
        ]
    )
    return return_record


def _available_allocation_unit(allocation, scan_value: str):
    trace = getattr(allocation, "traceability", None)
    marking_code = _normalized(getattr(trace, "marking_code", ""))
    barcode = _normalized(allocation.order_item.barcode or allocation.balance.barcode)
    if marking_code:
        if scan_value != marking_code:
            return None
    elif scan_value != barcode:
        return None
    used = set(
        FbsReturnUnit.objects.filter(source_allocation=allocation).values_list(
            "sequence", flat=True
        )
    )
    for sequence in range(1, int(allocation.qty_picked or 0) + 1):
        if sequence not in used:
            return sequence, barcode, trace
    return None


@transaction.atomic
def scan_return_unit(
    *, return_id: int, item_scan: str, performed_by
) -> FbsReturnUnit:
    _require_writes()
    actor = _actor(performed_by)
    return_record = (
        FbsReturn.objects.select_for_update()
        .select_related("order__profile__agency")
        .get(pk=return_id)
    )
    if return_record.status == FbsReturn.STATUS_COMPLETED:
        raise FbsReturnError("Возврат уже завершен.")
    if return_record.order_scan_confirmed_at is None:
        raise FbsReturnError("Сначала отсканируйте QR заказа маркетплейса.")
    if return_record.units.count() >= int(return_record.expected_qty or 0):
        raise FbsReturnError("Все ожидаемые единицы возврата уже отсканированы.")
    item_scan = _normalized(item_scan)
    if not item_scan:
        raise FbsReturnError("Отсканируйте штрихкод товара или КИЗ.")
    allocations = list(_picked_allocations(return_record.order_id, for_update=True))
    selected = None
    for allocation in allocations:
        candidate = _available_allocation_unit(allocation, item_scan)
        if candidate is not None:
            selected = (allocation, *candidate)
            break
    if selected is None:
        raise FbsReturnError(
            "Код не относится к не принятой единице подтвержденного FBS-отбора."
        )
    allocation, sequence, barcode, trace = selected
    unit = FbsReturnUnit(
        return_record=return_record,
        order_item=allocation.order_item,
        source_allocation=allocation,
        sequence=sequence,
        barcode=barcode,
        marking_code=_normalized(getattr(trace, "marking_code", "")),
        lot_code=_normalized(getattr(trace, "lot_code", "")),
        expiry_date=getattr(trace, "expiry_date", None),
        scan_value=item_scan,
        scanned_by=actor,
    )
    unit.full_clean()
    unit.save()
    if return_record.units.count() >= int(return_record.expected_qty or 0):
        return_record.status = FbsReturn.STATUS_INSPECTION
        return_record.save(update_fields=["status", "updated_at"])
    return unit


def _validate_photo(uploaded_file: UploadedFile) -> str:
    size = int(getattr(uploaded_file, "size", 0) or 0)
    if size <= 0:
        raise FbsReturnError("Файл фотографии пустой.")
    if size > MAX_RETURN_PHOTO_BYTES:
        raise FbsReturnError("Фотография должна быть не больше 10 МБ.")
    try:
        uploaded_file.seek(0)
        Image.open(uploaded_file).verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise FbsReturnError("Загрузите корректную фотографию JPEG, PNG или WebP.")
    uploaded_file.seek(0)
    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
    uploaded_file.seek(0)
    return digest.hexdigest()


@transaction.atomic
def add_return_photo(
    *, unit_id: int, uploaded_file: UploadedFile, kind: str, performed_by
) -> FbsReturnPhoto:
    _require_writes()
    actor = _actor(performed_by)
    unit = (
        FbsReturnUnit.objects.select_for_update()
        .select_related("return_record")
        .get(pk=unit_id)
    )
    if unit.return_record.status == FbsReturn.STATUS_COMPLETED:
        raise FbsReturnError("Нельзя добавить фото в завершенный возврат.")
    content_hash = _validate_photo(uploaded_file)
    existing = unit.photos.filter(content_hash=content_hash).first()
    if existing is not None:
        return existing
    if kind not in dict(FbsReturnPhoto.KIND_CHOICES):
        kind = FbsReturnPhoto.KIND_PRODUCT
    return FbsReturnPhoto.objects.create(
        unit=unit,
        kind=kind,
        file=uploaded_file,
        content_hash=content_hash,
        uploaded_by=actor,
    )


def _resolve_target_box(*, return_record: FbsReturn, box_scan: str) -> FbsBox:
    box_scan = _normalized(box_scan)
    if not box_scan:
        raise FbsReturnError("Для годного товара отсканируйте QR FBS-короба.")
    target_box = (
        FbsBox.objects.select_for_update()
        .select_related("pallet__cell__location")
        .filter(
            agency_id=return_record.order.profile.agency_id,
            box_code__iexact=box_scan,
            status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
            pallet__status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
            pallet__cell__is_active=True,
        )
        .first()
    )
    if target_box is None:
        raise FbsReturnError("Активный FBS-короб клиента не найден.")
    from .inventory import assert_box_unlocked

    assert_box_unlocked(target_box.id, for_execution=True)
    return target_box


@transaction.atomic
def inspect_return_unit(
    *,
    unit_id: int,
    condition: str,
    performed_by,
    condition_reason: str = "",
    target_box_scan: str = "",
) -> FbsReturnUnit:
    _require_writes()
    actor = _actor(performed_by)
    unit = (
        FbsReturnUnit.objects.select_for_update()
        .select_related("return_record__order__profile__agency")
        .get(pk=unit_id)
    )
    if unit.return_record.status == FbsReturn.STATUS_COMPLETED or unit.released_at:
        raise FbsReturnError("Решение по завершенному возврату нельзя изменить.")
    if condition not in {
        FbsReturnUnit.CONDITION_GOOD,
        FbsReturnUnit.CONDITION_DAMAGED,
        FbsReturnUnit.CONDITION_QUARANTINE,
    }:
        raise FbsReturnError("Выберите результат осмотра.")
    condition_reason = _normalized(condition_reason)
    target_box = None
    if condition == FbsReturnUnit.CONDITION_GOOD:
        if unit.expiry_date and unit.expiry_date < timezone.localdate():
            raise FbsReturnError(
                "Срок годности истек. Единицу можно направить только в повреждение или карантин."
            )
        target_box = _resolve_target_box(
            return_record=unit.return_record,
            box_scan=target_box_scan,
        )
    else:
        if not condition_reason:
            raise FbsReturnError("Для повреждения или карантина укажите причину.")
        if not unit.photos.exists():
            raise FbsReturnError("Для повреждения или карантина приложите фотографию.")
    unit.condition = condition
    unit.condition_reason = condition_reason
    unit.target_box = target_box
    unit.inspected_by = actor
    unit.inspected_at = timezone.now()
    unit.full_clean()
    unit.save(
        update_fields=[
            "condition",
            "condition_reason",
            "target_box",
            "inspected_by",
            "inspected_at",
            "updated_at",
        ]
    )
    return unit


def _release_good_unit(unit: FbsReturnUnit, *, actor, now) -> FbsStockBalance:
    source = FbsStockBalance.objects.select_for_update().get(
        pk=unit.source_allocation.balance_id
    )
    target_box = (
        FbsBox.objects.select_for_update()
        .select_related("pallet__cell__location")
        .get(pk=unit.target_box_id)
    )
    if target_box.agency_id != unit.return_record.order.profile.agency_id:
        raise FbsReturnError("FBS-короб возврата принадлежит другому клиенту.")
    if (
        target_box.status not in {FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE}
        or target_box.pallet.status
        not in {FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE}
        or not target_box.pallet.cell.is_active
    ):
        raise FbsReturnError("Место FBS-короба стало недоступно.")
    from .inventory import assert_box_unlocked

    assert_box_unlocked(target_box.id, for_execution=True)
    from fbs.models import FbsInventorySession, FbsStorageLock

    if FbsStorageLock.objects.filter(
        is_active=True,
        block_execution=True,
    ).filter(
        Q(scope_type=FbsInventorySession.SCOPE_ALL)
        | Q(
            scope_type=FbsInventorySession.SCOPE_AGENCY,
            agency_id=source.agency_id,
        )
        | Q(
            scope_type=FbsInventorySession.SCOPE_SKU,
            agency_id=source.agency_id,
            sku_id=source.sku_ref_id,
        )
    ).exists():
        raise FbsReturnError("Инвентаризация блокирует возврат этого SKU в FBS-остаток.")
    if unit.marking_code:
        balance = (
            FbsStockBalance.objects.select_for_update()
            .filter(
                agency_id=source.agency_id,
                marking_code=unit.marking_code,
            )
            .first()
        )
        if balance is None:
            balance = source
        if any(
            int(value or 0) > 0
            for value in (balance.qty, balance.available_qty, balance.reserved_qty)
        ):
            raise FbsReturnError("КИЗ уже числится в FBS-остатке.")
        balance.box = target_box
    else:
        balance = (
            FbsStockBalance.objects.select_for_update()
            .filter(box=target_box, identity_key=source.identity_key)
            .first()
        )
        if balance is None:
            balance = FbsStockBalance.objects.create(
                agency=source.agency,
                box=target_box,
                sku_ref=source.sku_ref,
                identity_key=source.identity_key,
                sku_code=source.sku_code,
                name=source.name,
                size=source.size,
                barcode=source.barcode,
                goods_type=source.goods_type,
                lot_code=source.lot_code,
                expiry_date=source.expiry_date,
                qty=0,
                available_qty=0,
                reserved_qty=0,
            )
    balance.qty = int(balance.qty or 0) + 1
    balance.available_qty = int(balance.available_qty or 0) + 1
    balance.full_clean()
    balance.save(update_fields=["box", "qty", "available_qty", "updated_at"])
    if target_box.status == FbsBox.STATUS_PLANNED:
        target_box.status = FbsBox.STATUS_ACTIVE
        target_box.save(update_fields=["status", "updated_at"])
    unit.released_balance = balance
    unit.released_at = now
    unit.save(update_fields=["released_balance", "released_at", "updated_at"])
    location = target_box.pallet.cell.location
    WarehouseEvent.objects.create(
        agency=source.agency,
        event_type="fbs_return_restocked",
        stock_context_type="fbs_return",
        stock_context_id=str(unit.return_record_id),
        container=target_box.source_container,
        source_document_type="fbs_return",
        source_document_id=str(unit.return_record_id),
        to_location=location,
        to_zone_code=str(location.zone_code or ""),
        qty=1,
        payload={
            "return_unit_id": unit.id,
            "order_id": unit.return_record.order_id,
            "target_box_id": target_box.id,
            "barcode": unit.barcode,
            "marking_code": unit.marking_code,
            "lot_code": unit.lot_code,
            "expiry_date": unit.expiry_date.isoformat() if unit.expiry_date else "",
        },
        performed_by=actor,
        performed_by_role="storekeeper",
        occurred_at=now,
    )
    return balance


@transaction.atomic
def complete_fbs_return(*, return_id: int, performed_by) -> FbsReturn:
    _require_writes()
    actor = _actor(performed_by)
    return_record = (
        FbsReturn.objects.select_for_update()
        .select_related("order__profile__agency")
        .get(pk=return_id)
    )
    if return_record.status == FbsReturn.STATUS_COMPLETED:
        return return_record
    units = list(
        FbsReturnUnit.objects.select_for_update()
        .select_related(
            "return_record__order__profile__agency",
            "source_allocation__balance",
            "target_box__pallet__cell__location",
        )
        .prefetch_related("photos")
        .filter(return_record=return_record)
        .order_by("id")
    )
    if len(units) != int(return_record.expected_qty or 0):
        raise FbsReturnError(
            f"Отсканировано {len(units)} из {return_record.expected_qty} шт."
        )
    pending = [unit for unit in units if unit.condition == FbsReturnUnit.CONDITION_PENDING]
    if pending:
        raise FbsReturnError("Осмотрите каждую отсканированную единицу.")
    for unit in units:
        if unit.condition == FbsReturnUnit.CONDITION_GOOD and not unit.target_box_id:
            raise FbsReturnError("Для каждого годного товара нужен FBS-короб.")
        if unit.condition in {
            FbsReturnUnit.CONDITION_DAMAGED,
            FbsReturnUnit.CONDITION_QUARANTINE,
        } and (not unit.condition_reason or not unit.photos.all()):
            raise FbsReturnError("Повреждение и карантин требуют причины и фотографии.")
    now = timezone.now()
    for unit in units:
        if unit.condition == FbsReturnUnit.CONDITION_GOOD:
            if unit.released_at is None:
                _release_good_unit(unit, actor=actor, now=now)
        else:
            WarehouseEvent.objects.create(
                agency=return_record.order.profile.agency,
                event_type="fbs_return_quarantined",
                stock_context_type="fbs_return",
                stock_context_id=str(return_record.id),
                source_document_type="fbs_return",
                source_document_id=str(return_record.id),
                qty=1,
                payload={
                    "return_unit_id": unit.id,
                    "order_id": return_record.order_id,
                    "condition": unit.condition,
                    "reason": unit.condition_reason,
                    "barcode": unit.barcode,
                    "marking_code": unit.marking_code,
                },
                performed_by=actor,
                performed_by_role="storekeeper",
                occurred_at=now,
            )
    return_record.status = FbsReturn.STATUS_COMPLETED
    return_record.completed_by = actor
    return_record.completed_at = now
    return_record.save(
        update_fields=["status", "completed_by", "completed_at", "updated_at"]
    )
    picked_qty = int(
        _picked_allocations(return_record.order_id).aggregate(total=Sum("qty_picked"))[
            "total"
        ]
        or 0
    )
    returned_qty = FbsReturnUnit.objects.filter(
        return_record__order_id=return_record.order_id,
        return_record__status=FbsReturn.STATUS_COMPLETED,
    ).count()
    order = FbsOrder.objects.select_for_update().get(pk=return_record.order_id)
    order.internal_status = (
        FbsOrder.STATUS_RETURNED
        if returned_qty >= picked_qty
        else FbsOrder.STATUS_RETURN_PENDING
    )
    order.save(update_fields=["internal_status", "updated_at"])
    try:
        from fbs.signals import return_completed

        responses = return_completed.send_robust(
            sender=complete_fbs_return,
            return_record=return_record,
            user=actor,
        )
        for _, response in responses:
            if isinstance(response, Exception):
                raise response
    except Exception:
        logger.exception("Unable to sync FBS return billing for %s", return_record.id)
    return return_record
