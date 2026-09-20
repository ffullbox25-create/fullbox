import base64
import json
from io import BytesIO

import fitz
from django.db import transaction
from django.http import QueryDict

from audit.models import log_sku_change, sku_snapshot
from processing_app.models import ProcessingPrintJob
from reachtruck.services.ui_flows import create_move_request_response
from shipping.models import ShippingOrder, ShippingOrderItem
from shipping.services import next_shipping_number
from sku.models import SKU, SKUBarcode

from .models import GoodsActionAudit


def log_goods_action(sku: SKU, action: str, user, **payload):
    GoodsActionAudit.objects.create(sku=sku, action=action, user=user, payload=payload)


@transaction.atomic
def save_sku_form(form, user):
    is_create = not form.instance.pk
    before = sku_snapshot(form.instance) if not is_create else None
    sku = form.save()
    log_sku_change(
        "create" if is_create else "update",
        sku,
        user=user,
        description="Создано в разделе «Товары»" if is_create else "Изменено в разделе «Товары»",
        snapshot={"before": before, "after": sku_snapshot(sku)} if before else sku_snapshot(sku),
    )
    return sku


@transaction.atomic
def add_barcode(sku: SKU, *, value: str, size: str = "", is_primary=False, user=None):
    if is_primary or not sku.barcodes.exists():
        sku.barcodes.update(is_primary=False)
        is_primary = True
    barcode = SKUBarcode.objects.create(
        sku=sku,
        value=value.strip(),
        size=(size or "").strip() or None,
        is_primary=is_primary,
    )
    log_sku_change("update", sku, user=user, description=f"Добавлен штрихкод {barcode.value}")
    log_goods_action(sku, "barcode_add", user, barcode=barcode.value)
    return barcode


@transaction.atomic
def delete_barcode(barcode: SKUBarcode, user):
    sku = barcode.sku
    was_primary = barcode.is_primary
    value = barcode.value
    barcode.delete()
    if was_primary:
        replacement = sku.barcodes.order_by("id").first()
        if replacement:
            replacement.is_primary = True
            replacement.save(update_fields=["is_primary"])
    log_sku_change("update", sku, user=user, description=f"Удален штрихкод {value}")
    log_goods_action(sku, "barcode_delete", user, barcode=value)


def create_transfer_draft(sku: SKU, qty: int, user, comment="") -> ShippingOrder:
    if not sku.agency_id:
        raise ValueError("У товара не указан клиент.")
    barcode = sku.barcodes.order_by("-is_primary", "id").values_list("value", flat=True).first() or ""
    with transaction.atomic():
        order = ShippingOrder.objects.create(
            number=next_shipping_number(),
            agency=sku.agency,
            created_by=user,
            status=ShippingOrder.STATUS_DRAFT,
            delivery_type=ShippingOrder.DELIVERY_TRANSFER,
            comment=(comment or "").strip(),
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku=sku,
            sku_code=sku.sku_code,
            name=sku.name,
            size=sku.size or "",
            barcode=barcode,
            goods_type=sku.type_tovar or "",
            qty_requested=qty,
        )
        log_goods_action(sku, "transfer_draft_create", user, order_id=order.pk, qty=qty)
    return order


def create_move_request(request, sku: SKU, *, location, qty: int, comment=""):
    original_post = request.POST
    data = QueryDict(mutable=True)
    data.update(
        {
            "agency_id": str(sku.agency_id or ""),
            "to_zone": location.zone_code,
            "to_row": str(location.row_no or ""),
            "to_section": str(location.section_no or ""),
            "to_tier": str(location.tier_no or ""),
            "to_cell": str(location.cell_no or ""),
            "requested_article": sku.sku_code,
            "requested_goods_type": sku.type_tovar or "",
            "requested_qty": str(qty),
            "pick_qty": str(qty),
            "requested_barcodes_json": json.dumps(
                list(sku.barcodes.values_list("value", flat=True)), ensure_ascii=False
            ),
            "comment": comment or f"Из карточки товара {sku.sku_code}",
        }
    )
    request.POST = data
    try:
        response = create_move_request_response(request)
    finally:
        request.POST = original_post
    payload = json.loads(response.content.decode("utf-8")) if response.content else {}
    if response.status_code >= 400 or not payload.get("ok"):
        raise ValueError(payload.get("error") or "Не удалось создать задание на перемещение.")
    log_goods_action(
        sku,
        "move_request_create",
        request.user,
        request_id=payload.get("request_id"),
        tasks_created=payload.get("tasks_created"),
        qty=qty,
        destination=location.location_code or location.display_name,
    )
    return payload


def _decode_label_image(data_url: str) -> bytes:
    value = str(data_url or "")
    if "," in value:
        value = value.split(",", 1)[1]
    return base64.b64decode(value, validate=True)


def build_label_pdf(data_url: str, *, width_mm: int, height_mm: int, copies: int) -> bytes:
    image_bytes = _decode_label_image(data_url)
    document = fitz.open()
    width_pt = width_mm * 72 / 25.4
    height_pt = height_mm * 72 / 25.4
    for _ in range(max(1, min(int(copies), 500))):
        page = document.new_page(width=width_pt, height=height_pt)
        page.insert_image(page.rect, stream=image_bytes, keep_proportion=False)
    result = document.tobytes(garbage=4, deflate=True)
    document.close()
    return result


def queue_label(sku: SKU, *, data_url: str, template_key: str, width_mm: int, height_mm: int, copies: int, user):
    barcode = sku.barcodes.order_by("-is_primary", "id").values_list("value", flat=True).first() or sku.sku_code
    encoded = base64.b64encode(_decode_label_image(data_url)).decode("ascii")
    job = ProcessingPrintJob.objects.create(
        order_id=f"goods-{sku.pk}",
        card_id=str(sku.pk),
        article=sku.sku_code,
        barcode=barcode,
        size=sku.size or "",
        label_png_base64=encoded,
        copies_count=max(1, min(int(copies), 500)),
        template_key=template_key,
        label_width_mm=width_mm,
        label_height_mm=height_mm,
        requested_by=user.get_full_name().strip() or user.username,
    )
    log_goods_action(sku, "label_queue", user, print_job_id=job.pk, copies=job.copies_count)
    return job
