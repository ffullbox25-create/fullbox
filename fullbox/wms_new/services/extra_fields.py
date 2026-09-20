from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_date
from django.utils.text import slugify

from ..models import (
    WmsNewEvent,
    WmsNewExtraFieldDefinition,
    WmsNewExtraFieldValue,
    WmsNewProduct,
)


class ExtraFieldOperationError(ValueError):
    pass


def _definition_snapshot(item: WmsNewExtraFieldDefinition) -> dict:
    return {
        "name": item.name,
        "code": item.code,
        "field_type": item.field_type,
        "is_active": item.is_active,
        "sort_order": item.sort_order,
        "options": item.options,
        "pilot_revision": item.pilot_revision,
    }


def _value_snapshot(item: WmsNewExtraFieldValue) -> dict:
    return {
        "definition_id": item.definition_id,
        "product_id": item.product_id,
        "value": item.value,
        "pilot_revision": item.pilot_revision,
    }


def _event(*, entity_type: str, item, action: str, actor, before: dict, after: dict) -> None:
    WmsNewEvent.objects.create(
        entity_type=entity_type,
        entity_id=item.id,
        action=action,
        actor=actor,
        before=before,
        after=after,
    )


def _definition_values(*, name, code, field_type, sort_order, options=None) -> dict:
    name = str(name or "").strip()
    code = str(code or "").strip() or slugify(name, allow_unicode=True)
    if not name:
        raise ExtraFieldOperationError("Название поля обязательно.")
    if not code:
        raise ExtraFieldOperationError("Код поля обязателен.")
    if field_type not in dict(WmsNewExtraFieldDefinition.TYPE_CHOICES):
        raise ExtraFieldOperationError("Неизвестный тип дополнительного поля.")
    try:
        sort_order = max(int(sort_order or 100), 0)
    except (TypeError, ValueError) as exc:
        raise ExtraFieldOperationError("Порядок должен быть целым числом.") from exc
    return {
        "name": name,
        "code": code,
        "field_type": field_type,
        "sort_order": sort_order,
        "options": list(options or []),
    }


def create_definition(*, actor=None, **values) -> WmsNewExtraFieldDefinition:
    normalized = _definition_values(**values)
    try:
        with transaction.atomic():
            item = WmsNewExtraFieldDefinition.objects.create(
                **normalized,
                is_active=True,
                pilot_revision=1,
                created_by=actor,
            )
            _event(
                entity_type="extra_field_definition",
                item=item,
                action="create",
                actor=actor,
                before={},
                after=_definition_snapshot(item),
            )
    except IntegrityError as exc:
        raise ExtraFieldOperationError("Поле с таким кодом уже существует.") from exc
    return item


def update_definition(*, definition_id: int, actor=None, **values) -> WmsNewExtraFieldDefinition:
    normalized = _definition_values(**values)
    with transaction.atomic():
        item = WmsNewExtraFieldDefinition.objects.select_for_update().get(pk=definition_id)
        before = _definition_snapshot(item)
        for field, value in normalized.items():
            setattr(item, field, value)
        item.pilot_revision += 1
        try:
            item.save()
        except IntegrityError as exc:
            raise ExtraFieldOperationError("Поле с таким кодом уже существует.") from exc
        _event(
            entity_type="extra_field_definition",
            item=item,
            action="update",
            actor=actor,
            before=before,
            after=_definition_snapshot(item),
        )
    return item


def set_definition_active(*, definition_id: int, active: bool, actor=None) -> WmsNewExtraFieldDefinition:
    with transaction.atomic():
        item = WmsNewExtraFieldDefinition.objects.select_for_update().get(pk=definition_id)
        before = _definition_snapshot(item)
        item.is_active = bool(active)
        item.pilot_revision += 1
        item.save(update_fields=("is_active", "pilot_revision", "updated_at"))
        _event(
            entity_type="extra_field_definition",
            item=item,
            action="activate" if active else "deactivate",
            actor=actor,
            before=before,
            after=_definition_snapshot(item),
        )
    return item


def _coerce_value(definition: WmsNewExtraFieldDefinition, raw):
    if raw in (None, ""):
        return None
    if definition.field_type == definition.TYPE_BOOLEAN:
        return str(raw).strip().lower() in {"1", "true", "yes", "да", "on"}
    if definition.field_type == definition.TYPE_NUMBER:
        try:
            value = Decimal(str(raw).strip().replace(",", "."))
        except InvalidOperation as exc:
            raise ExtraFieldOperationError(f"Поле «{definition.name}» должно быть числом.") from exc
        return format(value.normalize(), "f")
    if definition.field_type == definition.TYPE_DATE:
        value = parse_date(str(raw).strip())
        if not value:
            raise ExtraFieldOperationError(f"Поле «{definition.name}» должно быть датой.")
        return value.isoformat()
    return str(raw).strip()


def set_product_value(
    *,
    definition_id: int,
    product_id: int,
    raw_value,
    actor=None,
) -> WmsNewExtraFieldValue:
    with transaction.atomic():
        definition = WmsNewExtraFieldDefinition.objects.select_for_update().get(
            pk=definition_id,
            is_active=True,
        )
        product = WmsNewProduct.objects.get(pk=product_id, is_archived=False)
        item = WmsNewExtraFieldValue.objects.select_for_update().filter(
            definition=definition,
            product=product,
        ).first()
        before = _value_snapshot(item) if item else {}
        value = _coerce_value(definition, raw_value)
        if item is None:
            item = WmsNewExtraFieldValue.objects.create(
                definition=definition,
                product=product,
                value=value,
                pilot_revision=1,
                updated_by=actor,
            )
        else:
            item.value = value
            item.pilot_revision += 1
            item.updated_by = actor
            item.save(update_fields=("value", "pilot_revision", "updated_by", "updated_at"))
        _event(
            entity_type="extra_field_value",
            item=item,
            action="set_value",
            actor=actor,
            before=before,
            after=_value_snapshot(item),
        )
    return item
