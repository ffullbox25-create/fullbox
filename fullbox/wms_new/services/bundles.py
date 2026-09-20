from __future__ import annotations

from django.db import transaction

from sklad.models import WarehouseLocation

from ..models import (
    WmsNewBundleComponent,
    WmsNewBundleOperation,
    WmsNewBundleStock,
    WmsNewEvent,
    WmsNewProduct,
)


class BundleOperationError(ValueError):
    pass


def _quantity(value) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise BundleOperationError("Количество должно быть целым числом.") from exc
    if result <= 0:
        raise BundleOperationError("Количество должно быть больше нуля.")
    return result


def _snapshot(bundle: WmsNewProduct) -> list[dict]:
    return [
        {
            "product_id": item.component_id,
            "article": item.component.article,
            "name": item.component.name,
            "quantity": item.quantity,
        }
        for item in bundle.bundle_components.select_related("component").all()
    ]


def _event(operation: WmsNewBundleOperation) -> None:
    WmsNewEvent.objects.create(
        entity_type="bundle",
        entity_id=operation.bundle_id,
        action=operation.action,
        actor=operation.actor,
        after={
            "quantity": operation.quantity,
            "location_id": operation.location_id,
            "components": operation.component_snapshot,
            "bundle_stock": operation.bundle.stock_on_hand,
        },
    )


@transaction.atomic
def define_bundle(*, bundle_id: int, components: list[tuple[int, int]], actor=None) -> WmsNewProduct:
    bundle = WmsNewProduct.objects.select_for_update().get(pk=bundle_id, is_archived=False)
    if bundle.is_bundle or bundle.bundle_components.exists():
        raise BundleOperationError("Состав набора уже создан и не может быть изменен.")
    normalized = {}
    for component_id, quantity in components:
        component_id = int(component_id)
        if component_id in normalized:
            raise BundleOperationError("Один товар нельзя добавить в состав набора дважды.")
        normalized[component_id] = _quantity(quantity)
    if not normalized:
        raise BundleOperationError("Добавьте хотя бы один товар в состав набора.")
    if bundle.id in normalized:
        raise BundleOperationError("Товар не может входить сам в себя.")
    component_rows = list(
        WmsNewProduct.objects.select_for_update().filter(
            id__in=normalized,
            agency_id=bundle.agency_id,
            is_archived=False,
            is_bundle=False,
        )
    )
    if len(component_rows) != len(normalized):
        raise BundleOperationError("Часть товаров состава не найдена или относится к другому партнеру.")
    for component in component_rows:
        WmsNewBundleComponent.objects.create(
            bundle=bundle,
            component=component,
            quantity=normalized[component.id],
        )
    bundle.is_bundle = True
    bundle.pilot_revision += 1
    bundle.save(update_fields=("is_bundle", "pilot_revision", "updated_at"))
    operation = WmsNewBundleOperation.objects.create(
        bundle=bundle,
        action=WmsNewBundleOperation.ACTION_DEFINE,
        quantity=1,
        component_snapshot=_snapshot(bundle),
        actor=actor,
    )
    _event(operation)
    return bundle


@transaction.atomic
def assemble_bundle(
    *, bundle_id: int, quantity, location_id: int, actor=None
) -> WmsNewBundleOperation:
    amount = _quantity(quantity)
    bundle = WmsNewProduct.objects.select_for_update().get(
        pk=bundle_id, is_bundle=True, is_archived=False
    )
    location = WarehouseLocation.objects.get(pk=location_id, is_active=True)
    components = list(
        bundle.bundle_components.select_related("component").select_for_update()
    )
    if not components:
        raise BundleOperationError("У набора не определен состав.")
    for item in components:
        needed = item.quantity * amount
        if item.component.stock_free < needed or item.component.stock_on_hand < needed:
            raise BundleOperationError(
                f"Недостаточно товара «{item.component.name}»: требуется {needed}."
            )
    for item in components:
        needed = item.quantity * amount
        product = item.component
        product.stock_free -= needed
        product.stock_on_hand -= needed
        product.pilot_revision += 1
        product.save(update_fields=("stock_free", "stock_on_hand", "pilot_revision", "updated_at"))
    bundle.stock_on_hand += amount
    bundle.stock_free += amount
    bundle.pilot_revision += 1
    bundle.save(update_fields=("stock_on_hand", "stock_free", "pilot_revision", "updated_at"))
    stock, _ = WmsNewBundleStock.objects.select_for_update().get_or_create(
        bundle=bundle,
        location=location,
    )
    stock.quantity += amount
    stock.save(update_fields=("quantity", "updated_at"))
    operation = WmsNewBundleOperation.objects.create(
        bundle=bundle,
        action=WmsNewBundleOperation.ACTION_ASSEMBLE,
        quantity=amount,
        location=location,
        component_snapshot=_snapshot(bundle),
        actor=actor,
    )
    _event(operation)
    return operation


@transaction.atomic
def disassemble_bundle(
    *, bundle_id: int, quantity, location_id: int, actor=None
) -> WmsNewBundleOperation:
    amount = _quantity(quantity)
    bundle = WmsNewProduct.objects.select_for_update().get(
        pk=bundle_id, is_bundle=True, is_archived=False
    )
    try:
        stock = WmsNewBundleStock.objects.select_for_update().select_related("location").get(
            bundle=bundle,
            location_id=location_id,
        )
    except WmsNewBundleStock.DoesNotExist as exc:
        raise BundleOperationError("На выбранном месте нет этого набора.") from exc
    if stock.quantity < amount or bundle.stock_on_hand < amount:
        raise BundleOperationError("На выбранном месте недостаточно наборов.")
    components = list(
        bundle.bundle_components.select_related("component").select_for_update()
    )
    stock.quantity -= amount
    stock.save(update_fields=("quantity", "updated_at"))
    bundle.stock_on_hand -= amount
    bundle.stock_free = max(0, bundle.stock_free - amount)
    bundle.pilot_revision += 1
    bundle.save(update_fields=("stock_on_hand", "stock_free", "pilot_revision", "updated_at"))
    for item in components:
        restored = item.quantity * amount
        product = item.component
        product.stock_on_hand += restored
        product.stock_free += restored
        product.pilot_revision += 1
        product.save(update_fields=("stock_on_hand", "stock_free", "pilot_revision", "updated_at"))
    operation = WmsNewBundleOperation.objects.create(
        bundle=bundle,
        action=WmsNewBundleOperation.ACTION_DISASSEMBLE,
        quantity=amount,
        location=stock.location,
        component_snapshot=_snapshot(bundle),
        actor=actor,
    )
    _event(operation)
    return operation
