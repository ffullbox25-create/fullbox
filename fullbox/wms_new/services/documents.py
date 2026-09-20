from __future__ import annotations

from collections import defaultdict

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from sklad.models import WarehouseLocation

from ..models import (
    WmsNewBox,
    WmsNewBoxItem,
    WmsNewDocument,
    WmsNewDocumentItem,
    WmsNewEvent,
    WmsNewMovement,
    WmsNewProduct,
)


class DocumentOperationError(ValueError):
    pass


def _actor_name(actor) -> str:
    if actor is None:
        return ""
    return str(actor.get_full_name() or actor.get_username() or actor).strip()


def _location_name(location: WarehouseLocation | None) -> str:
    if location is None:
        return ""
    return location.display_name or location.location_code or location.zone_code


def _recalculate_document(document: WmsNewDocument) -> None:
    totals = document.items.aggregate(units=Sum("quantity"))
    document.sku_count = document.items.values("product_id", "article", "product_name").distinct().count()
    document.unit_count = int(totals["units"] or 0)


def _recalculate_box(box: WmsNewBox) -> None:
    totals = box.items.aggregate(
        qty=Sum("qty"),
        available=Sum("available_qty"),
        reserved=Sum("reserved_qty"),
    )
    box.stock_on_hand = int(totals["qty"] or 0)
    box.stock_free = int(totals["available"] or 0)
    box.reserved_qty = int(totals["reserved"] or 0)
    box.sku_count = box.items.filter(qty__gt=0).values("sku_code").distinct().count()
    box.marking_count = box.items.exclude(marking_code="").filter(qty__gt=0).count()
    box.pilot_revision += 1
    box.save(
        update_fields=(
            "stock_on_hand",
            "stock_free",
            "reserved_qty",
            "sku_count",
            "marking_count",
            "pilot_revision",
            "updated_at",
        )
    )


@transaction.atomic
def create_document(*, document_type: str, agency_id: int, actor=None) -> WmsNewDocument:
    if document_type not in dict(WmsNewDocument.TYPE_CHOICES):
        raise DocumentOperationError("Неизвестный тип документа.")
    document = WmsNewDocument.objects.create(
        document_type=document_type,
        agency_id=agency_id,
        status=WmsNewDocument.STATUS_DRAFT,
        basis="Ручной документ FBS-NEW",
        is_manual=True,
        pilot_revision=1,
        created_by=actor,
        source_created_at=timezone.now(),
    )
    WmsNewEvent.objects.create(
        entity_type="document",
        entity_id=document.id,
        action="create",
        actor=actor,
        after={"type": document_type, "agency_id": agency_id},
    )
    return document


def _editable_document(document_id: int) -> WmsNewDocument:
    document = WmsNewDocument.objects.select_for_update().get(pk=document_id)
    if not document.is_manual:
        raise DocumentOperationError("Документ перенесен из рабочего контура и доступен только для просмотра.")
    if document.status != WmsNewDocument.STATUS_DRAFT:
        raise DocumentOperationError("Изменять можно только документ в статусе «Черновик».")
    return document


@transaction.atomic
def add_item(
    *,
    document_id: int,
    product_id: int,
    quantity,
    location_id: int | None = None,
    unit_price=0,
    actor=None,
) -> WmsNewDocumentItem:
    document = _editable_document(document_id)
    try:
        amount = int(quantity)
    except (TypeError, ValueError) as exc:
        raise DocumentOperationError("Количество должно быть целым числом.") from exc
    if amount <= 0:
        raise DocumentOperationError("Количество должно быть больше нуля.")
    product = WmsNewProduct.objects.select_for_update().get(
        pk=product_id,
        agency_id=document.agency_id,
        is_archived=False,
    )
    location = None
    if location_id:
        location = WarehouseLocation.objects.filter(pk=location_id, is_active=True).first()
        if location is None:
            raise DocumentOperationError("Место хранения не найдено.")
    item = WmsNewDocumentItem.objects.create(
        document=document,
        product=product,
        location=location,
        product_name=product.name,
        article=product.article,
        barcode=product.barcode,
        location_name=_location_name(location),
        quantity=amount,
        unit_price=unit_price or 0,
    )
    _recalculate_document(document)
    document.pilot_revision += 1
    document.save(update_fields=("sku_count", "unit_count", "pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="document",
        entity_id=document.id,
        action="add_item",
        actor=actor,
        after={"item_id": item.id, "product_id": product.id, "quantity": amount},
    )
    return item


@transaction.atomic
def remove_item(*, document_id: int, item_id: int, actor=None) -> None:
    document = _editable_document(document_id)
    item = document.items.get(pk=item_id)
    before = {"item_id": item.id, "product_id": item.product_id, "quantity": item.quantity}
    item.delete()
    _recalculate_document(document)
    document.pilot_revision += 1
    document.save(update_fields=("sku_count", "unit_count", "pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="document",
        entity_id=document.id,
        action="remove_item",
        actor=actor,
        before=before,
    )


@transaction.atomic
def update_comments(*, document_id: int, comment: str, internal_comment: str, actor=None) -> WmsNewDocument:
    document = _editable_document(document_id)
    before = {"comment": document.comment, "internal_comment": document.internal_comment}
    document.comment = str(comment or "").strip()
    document.internal_comment = str(internal_comment or "").strip()
    document.pilot_revision += 1
    document.save(update_fields=("comment", "internal_comment", "pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="document",
        entity_id=document.id,
        action="update_comments",
        actor=actor,
        before=before,
        after={"comment": document.comment, "internal_comment": document.internal_comment},
    )
    return document


def _receipt_box(document: WmsNewDocument, item: WmsNewDocumentItem, actor=None) -> WmsNewBox:
    suffix = item.location_id or "unplaced"
    code = f"FBN-DOC-{document.id}-{suffix}"
    box, _ = WmsNewBox.objects.select_for_update().get_or_create(
        agency_id=document.agency_id,
        code=code,
        defaults={
            "location": item.location,
            "location_code": item.location_name,
            "zone_code": item.location.zone_code if item.location else "",
            "is_manual": True,
            "pilot_revision": 1,
            "created_by": actor,
        },
    )
    return box


def _post_receipt_item(document: WmsNewDocument, item: WmsNewDocumentItem, actor=None) -> None:
    product = WmsNewProduct.objects.select_for_update().get(pk=item.product_id)
    product.stock_on_hand += item.quantity
    product.stock_free += item.quantity
    product.pilot_revision += 1
    product.save(update_fields=("stock_on_hand", "stock_free", "pilot_revision", "updated_at"))
    box = _receipt_box(document, item, actor=actor)
    box_item = (
        WmsNewBoxItem.objects.select_for_update()
        .filter(box=box, product=product, barcode=item.barcode, marking_code="")
        .first()
    )
    if box_item is None:
        WmsNewBoxItem.objects.create(
            box=box,
            product=product,
            agency_id=document.agency_id,
            sku_code=item.article,
            product_name=item.product_name,
            size=product.size,
            barcode=item.barcode,
            qty=item.quantity,
            available_qty=item.quantity,
            pilot_revision=1,
            source_snapshot={"origin": "wms_new_document", "document_id": document.id},
        )
    else:
        box_item.qty += item.quantity
        box_item.available_qty += item.quantity
        box_item.pilot_revision += 1
        box_item.save(update_fields=("qty", "available_qty", "pilot_revision", "updated_at"))
    _recalculate_box(box)
    WmsNewMovement.objects.create(
        agency_id=document.agency_id,
        product=product,
        product_name=item.product_name,
        article=item.article,
        action=WmsNewMovement.ACTION_RECEIPT,
        target_location=item.location,
        target_location_name=item.location_name,
        quantity=item.quantity,
        balance_after=product.stock_on_hand,
        boxes_count=1,
        information=f"Приходный документ №{document.id}",
        source_event_type="wms_new_document",
        actor=actor,
        actor_name=_actor_name(actor),
        occurred_at=timezone.now(),
        payload={"document_id": document.id, "item_id": item.id},
        is_manual=True,
    )


def _post_writeoff_item(document: WmsNewDocument, item: WmsNewDocumentItem, actor=None) -> None:
    product = WmsNewProduct.objects.select_for_update().get(pk=item.product_id)
    box_items = WmsNewBoxItem.objects.select_for_update().filter(
        product=product,
        box__agency_id=document.agency_id,
        box__status=WmsNewBox.STATUS_ACTIVE,
        available_qty__gt=0,
    )
    if item.location_id:
        box_items = box_items.filter(box__location_id=item.location_id)
    box_items = list(box_items.select_related("box").order_by("box_id", "id"))
    available = sum(row.available_qty for row in box_items)
    if available < item.quantity:
        raise DocumentOperationError(
            f"Для «{item.product_name}» доступно {available} ед., требуется {item.quantity}."
        )
    remaining = item.quantity
    touched = {}
    for row in box_items:
        if remaining <= 0:
            break
        amount = min(row.available_qty, remaining)
        row.qty -= amount
        row.available_qty -= amount
        row.pilot_revision += 1
        row.save(update_fields=("qty", "available_qty", "pilot_revision", "updated_at"))
        touched[row.box_id] = row.box
        remaining -= amount
    for box in touched.values():
        _recalculate_box(box)
    if product.stock_on_hand < item.quantity or product.stock_free < item.quantity:
        raise DocumentOperationError(f"Недостаточный агрегированный остаток по товару «{item.product_name}».")
    product.stock_on_hand -= item.quantity
    product.stock_free -= item.quantity
    product.pilot_revision += 1
    product.save(update_fields=("stock_on_hand", "stock_free", "pilot_revision", "updated_at"))
    WmsNewMovement.objects.create(
        agency_id=document.agency_id,
        product=product,
        product_name=item.product_name,
        article=item.article,
        action=WmsNewMovement.ACTION_WRITEOFF,
        source_location=item.location,
        source_location_name=item.location_name,
        quantity=-item.quantity,
        balance_after=product.stock_on_hand,
        boxes_count=len(touched),
        information=f"Расходный документ №{document.id}",
        source_event_type="wms_new_document",
        actor=actor,
        actor_name=_actor_name(actor),
        occurred_at=timezone.now(),
        payload={"document_id": document.id, "item_id": item.id},
        is_manual=True,
    )


@transaction.atomic
def post_document(*, document_id: int, actor=None) -> WmsNewDocument:
    document = _editable_document(document_id)
    items = list(document.items.select_related("product", "location").order_by("id"))
    if not items:
        raise DocumentOperationError("Добавьте хотя бы один товар.")
    if any(item.product_id is None for item in items):
        raise DocumentOperationError("В документе есть товар без карточки FBS-NEW.")
    for item in items:
        if document.document_type == WmsNewDocument.TYPE_RECEIPT:
            _post_receipt_item(document, item, actor=actor)
        else:
            _post_writeoff_item(document, item, actor=actor)
    document.status = WmsNewDocument.STATUS_POSTED
    document.posted_at = timezone.now()
    document.posted_by = actor
    document.pilot_revision += 1
    document.save(
        update_fields=("status", "posted_at", "posted_by", "pilot_revision", "updated_at")
    )
    WmsNewEvent.objects.create(
        entity_type="document",
        entity_id=document.id,
        action="post",
        actor=actor,
        after={"type": document.document_type, "unit_count": document.unit_count},
    )
    return document


@transaction.atomic
def cancel_document(*, document_id: int, actor=None) -> WmsNewDocument:
    document = _editable_document(document_id)
    document.status = WmsNewDocument.STATUS_CANCELLED
    document.cancelled_at = timezone.now()
    document.pilot_revision += 1
    document.save(update_fields=("status", "cancelled_at", "pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="document",
        entity_id=document.id,
        action="cancel",
        actor=actor,
        after={"status": document.status},
    )
    return document
