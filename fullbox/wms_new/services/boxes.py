from __future__ import annotations

from django.db import transaction

from ..models import WmsNewBox, WmsNewEvent


class BoxOperationError(ValueError):
    pass


def _actor(actor):
    return actor if getattr(actor, "is_authenticated", False) else None


def _event(box: WmsNewBox, action: str, *, actor=None, before=None):
    WmsNewEvent.objects.create(
        entity_type="box",
        entity_id=box.id,
        action=action,
        actor=_actor(actor),
        before=before or {},
        after={
            "code": box.code,
            "container_type": box.source_context_type or "box",
            "status": box.status,
            "location_id": box.location_id,
            "location_code": box.location_code,
            "pilot_revision": box.pilot_revision,
        },
        metadata={"agency_id": box.agency_id},
    )


@transaction.atomic
def create_box(
    *,
    agency,
    code: str,
    container_type: str = "box",
    location=None,
    gross_weight_g=None,
    width_mm=None,
    height_mm=None,
    depth_mm=None,
    actor=None,
) -> WmsNewBox:
    container_type = str(container_type or "box").strip().lower()
    if container_type not in {"box", "pallet"}:
        raise BoxOperationError("Неизвестный тип контейнера.")
    code = str(code or "").strip()
    if not code:
        raise BoxOperationError("Укажите код короба.")
    if WmsNewBox.objects.filter(agency=agency, code__iexact=code).exists():
        raise BoxOperationError("Короб с таким кодом уже существует в WMS NEW.")
    values = {}
    for field, value in {
        "gross_weight_g": gross_weight_g,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "depth_mm": depth_mm,
    }.items():
        if value in (None, ""):
            values[field] = None
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise BoxOperationError("Вес и размеры должны быть целыми числами.") from exc
        if parsed < 0:
            raise BoxOperationError("Вес и размеры не могут быть отрицательными.")
        values[field] = parsed
    box = WmsNewBox.objects.create(
        agency=agency,
        code=code,
        location=location,
        location_code=(location.location_code if location else ""),
        zone_code=(location.zone_code if location else ""),
        source_context_type=container_type,
        is_manual=True,
        pilot_revision=1,
        created_by=_actor(actor),
        **values,
    )
    _event(box, "box_created", actor=actor)
    return box


@transaction.atomic
def update_box(
    *,
    box_id: int,
    code: str,
    location=None,
    gross_weight_g=None,
    width_mm=None,
    height_mm=None,
    depth_mm=None,
    actor=None,
) -> WmsNewBox:
    box = WmsNewBox.objects.select_for_update().get(pk=box_id)
    code = str(code or "").strip()
    if not code:
        raise BoxOperationError("Укажите код короба.")
    if WmsNewBox.objects.filter(agency=box.agency, code__iexact=code).exclude(pk=box.id).exists():
        raise BoxOperationError("Короб с таким кодом уже существует в WMS NEW.")
    before = {
        "code": box.code,
        "status": box.status,
        "location_id": box.location_id,
        "location_code": box.location_code,
    }
    values = {
        "gross_weight_g": gross_weight_g,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "depth_mm": depth_mm,
    }
    for field, value in values.items():
        if value in (None, ""):
            setattr(box, field, None)
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise BoxOperationError("Вес и размеры должны быть целыми числами.") from exc
        if parsed < 0:
            raise BoxOperationError("Вес и размеры не могут быть отрицательными.")
        setattr(box, field, parsed)
    box.code = code
    box.location = location
    box.location_code = location.location_code if location else ""
    box.zone_code = location.zone_code if location else ""
    box.pilot_revision += 1
    box.save()
    _event(box, "box_updated", actor=actor, before=before)
    return box


@transaction.atomic
def set_box_archived(*, box_id: int, archived: bool, actor=None) -> WmsNewBox:
    box = WmsNewBox.objects.select_for_update().get(pk=box_id)
    if archived and box.stock_on_hand:
        raise BoxOperationError("Нельзя архивировать короб с остатком.")
    before = {"status": box.status}
    box.status = WmsNewBox.STATUS_ARCHIVED if archived else WmsNewBox.STATUS_ACTIVE
    box.pilot_revision += 1
    box.save(update_fields=("status", "pilot_revision", "updated_at"))
    _event(
        box,
        "box_archived" if archived else "box_restored",
        actor=actor,
        before=before,
    )
    return box
