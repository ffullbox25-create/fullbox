from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction

from ..models import WmsNewEvent, WmsNewProduct


class ProductOperationError(ValueError):
    pass


def _decimal(value, *, field: str) -> Decimal:
    raw = str(value or "0").strip().replace(",", ".")
    try:
        result = Decimal(raw)
    except InvalidOperation as exc:
        raise ProductOperationError(f"Поле «{field}» должно быть числом.") from exc
    if result < 0:
        raise ProductOperationError(f"Поле «{field}» не может быть отрицательным.")
    return result


def _snapshot(product: WmsNewProduct) -> dict:
    return {
        "agency_id": product.agency_id,
        "name": product.name,
        "article": product.article,
        "barcode": product.barcode,
        "category": product.category,
        "weight_grams": str(product.weight_grams),
        "dimensions": product.dimensions,
        "is_bundle": product.is_bundle,
        "is_archived": product.is_archived,
        "stock_on_hand": product.stock_on_hand,
        "stock_free": product.stock_free,
        "fbo_reserved": product.fbo_reserved,
        "fbs_reserved": product.fbs_reserved,
        "internal_reserved": product.internal_reserved,
        "merged_into_id": product.merged_into_id,
        "pilot_revision": product.pilot_revision,
    }


def _write_event(*, product: WmsNewProduct, action: str, actor, before: dict, metadata=None) -> None:
    WmsNewEvent.objects.create(
        entity_type="product",
        entity_id=product.id,
        action=action,
        actor=actor,
        before=before,
        after=_snapshot(product),
        metadata=metadata or {},
    )


def create_product(
    *,
    agency,
    name: str,
    article: str,
    barcode: str = "",
    color: str = "",
    weight_grams=0,
    size: str = "",
    category: str = "",
    width_cm=0,
    depth_cm=0,
    height_cm=0,
    image_url: str = "",
    internal_notes: str = "",
    description: str = "",
    is_bundle: bool = False,
    actor=None,
) -> WmsNewProduct:
    name = str(name or "").strip()
    article = str(article or "").strip()
    if not name:
        raise ProductOperationError("Название обязательно.")
    if not article:
        raise ProductOperationError("Артикул обязателен.")
    values = {
        "agency": agency,
        "name": name,
        "article": article,
        "barcode": str(barcode or "").strip(),
        "color": str(color or "").strip(),
        "weight_grams": _decimal(weight_grams, field="Вес"),
        "size": str(size or "").strip(),
        "category": str(category or "").strip(),
        "width_cm": _decimal(width_cm, field="Ширина"),
        "depth_cm": _decimal(depth_cm, field="Глубина"),
        "height_cm": _decimal(height_cm, field="Высота"),
        "image_url": str(image_url or "").strip(),
        "internal_notes": str(internal_notes or "").strip(),
        "description": str(description or "").strip(),
        "is_bundle": bool(is_bundle),
        "is_manual": True,
        "pilot_revision": 1,
        "created_by": actor,
    }
    try:
        with transaction.atomic():
            product = WmsNewProduct.objects.create(**values)
            _write_event(product=product, action="create", actor=actor, before={})
    except IntegrityError as exc:
        raise ProductOperationError("У партнера уже есть активный товар с таким артикулом.") from exc
    return product


def update_product(*, product_id: int, actor=None, **values) -> WmsNewProduct:
    with transaction.atomic():
        product = WmsNewProduct.objects.select_for_update().get(pk=product_id, is_archived=False)
        before = _snapshot(product)
        for field in (
            "name",
            "article",
            "barcode",
            "color",
            "size",
            "category",
            "image_url",
            "internal_notes",
            "description",
        ):
            if field in values:
                setattr(product, field, str(values[field] or "").strip())
        if not product.name or not product.article:
            raise ProductOperationError("Название и артикул обязательны.")
        for field, label in (
            ("weight_grams", "Вес"),
            ("width_cm", "Ширина"),
            ("depth_cm", "Глубина"),
            ("height_cm", "Высота"),
        ):
            if field in values:
                setattr(product, field, _decimal(values[field], field=label))
        if "is_bundle" in values:
            product.is_bundle = bool(values["is_bundle"])
        product.pilot_revision += 1
        try:
            product.save()
        except IntegrityError as exc:
            raise ProductOperationError("У партнера уже есть активный товар с таким артикулом.") from exc
        _write_event(product=product, action="update", actor=actor, before=before)
    return product


def assign_category(*, product_ids: list[int], category: str, actor=None) -> int:
    category = str(category or "").strip()
    if not category:
        raise ProductOperationError("Укажите категорию.")
    changed = 0
    with transaction.atomic():
        products = list(
            WmsNewProduct.objects.select_for_update().filter(pk__in=product_ids, is_archived=False)
        )
        if len(products) != len(set(product_ids)):
            raise ProductOperationError("Часть выбранных товаров не найдена.")
        for product in products:
            before = _snapshot(product)
            product.category = category
            product.pilot_revision += 1
            product.save(update_fields=("category", "pilot_revision", "updated_at"))
            _write_event(product=product, action="assign_category", actor=actor, before=before)
            changed += 1
    return changed


def merge_products(*, product_ids: list[int], target_id: int | None = None, actor=None) -> WmsNewProduct:
    unique_ids = list(dict.fromkeys(int(value) for value in product_ids))
    if len(unique_ids) < 2:
        raise ProductOperationError("Для объединения выберите минимум два товара.")
    target_id = int(target_id or unique_ids[0])
    if target_id not in unique_ids:
        raise ProductOperationError("Основной товар должен входить в выбранный список.")

    with transaction.atomic():
        products = list(
            WmsNewProduct.objects.select_for_update()
            .filter(pk__in=unique_ids, is_archived=False)
            .order_by("id")
        )
        if len(products) != len(unique_ids):
            raise ProductOperationError("Часть выбранных товаров не найдена.")
        if len({product.agency_id for product in products}) != 1:
            raise ProductOperationError("Объединять можно только товары одного партнера.")
        target = next(product for product in products if product.id == target_id)
        before_target = _snapshot(target)
        sources = [product for product in products if product.id != target.id]
        for field in (
            "marking_count",
            "stock_on_hand",
            "stock_free",
            "fbo_reserved",
            "fbs_reserved",
            "internal_reserved",
            "expected_qty",
        ):
            setattr(target, field, int(getattr(target, field) or 0) + sum(int(getattr(item, field) or 0) for item in sources))
        target.pilot_revision += 1
        target.save()
        _write_event(
            product=target,
            action="merge_target",
            actor=actor,
            before=before_target,
            metadata={"source_product_ids": [item.id for item in sources]},
        )
        for source in sources:
            before_source = _snapshot(source)
            source.is_archived = True
            source.merged_into = target
            source.pilot_revision += 1
            source.save(update_fields=("is_archived", "merged_into", "pilot_revision", "updated_at"))
            _write_event(
                product=source,
                action="merge_source",
                actor=actor,
                before=before_source,
                metadata={"target_product_id": target.id},
            )
    return target
