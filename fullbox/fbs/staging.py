from __future__ import annotations

from django.db import transaction

from fbs.exceptions import FbsReplenishmentError
from sklad.models import WarehouseContainer, WarehouseLocation


FBS_STAGING_CONTEXT_TYPE = "fbs_staging"


def movement_staging_container_code(movement_or_id) -> str:
    movement_id = int(getattr(movement_or_id, "pk", movement_or_id) or 0)
    if movement_id <= 0:
        raise FbsReplenishmentError("Не удалось определить заявку для временного FBS-короба.")
    return f"FBS-STAGE-MOV-{movement_id:06d}"


def ensure_movement_staging_container(
    *,
    movement,
    location: WarehouseLocation,
    created_by=None,
) -> WarehouseContainer:
    if location is None or str(location.zone_code or "").strip().upper() != "FBS-PREP":
        raise FbsReplenishmentError("Для временного короба не настроена зона подготовки FBS.")
    code = movement_staging_container_code(movement)
    container = (
        WarehouseContainer.objects.select_for_update()
        .filter(agency=movement.agency, container_code=code)
        .first()
    )
    actor = created_by if getattr(created_by, "is_authenticated", False) else None
    if container is None:
        return WarehouseContainer.objects.create(
            agency=movement.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=code,
            current_location=location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STAGING_CONTEXT_TYPE,
            source_context_id=str(movement.pk),
            created_by=actor,
        )
    if (
        container.container_type != WarehouseContainer.TYPE_BOX
        or container.source_context_type != FBS_STAGING_CONTEXT_TYPE
        or str(container.source_context_id or "") != str(movement.pk)
    ):
        raise FbsReplenishmentError(
            f"Код {code} уже используется другим складским контейнером."
        )
    updates = []
    if container.current_location_id != location.id:
        container.current_location = location
        updates.append("current_location")
    if container.status != WarehouseContainer.STATUS_ACTIVE:
        container.status = WarehouseContainer.STATUS_ACTIVE
        updates.append("status")
    if updates:
        container.save(update_fields=[*updates, "updated_at"])
    return container


@transaction.atomic
def archive_movement_staging_container(*, movement) -> None:
    code = movement_staging_container_code(movement)
    container = (
        WarehouseContainer.objects.select_for_update()
        .filter(
            agency=movement.agency,
            container_code=code,
            source_context_type=FBS_STAGING_CONTEXT_TYPE,
            source_context_id=str(movement.pk),
        )
        .first()
    )
    if container is None or container.status == WarehouseContainer.STATUS_ARCHIVED:
        return
    container.status = WarehouseContainer.STATUS_ARCHIVED
    container.save(update_fields=["status", "updated_at"])
