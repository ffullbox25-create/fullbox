from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from fbs.exceptions import FbsFeatureDisabled, FbsHandoverError
from fbs.flags import feature_enabled
from fbs.models import (
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsHandoverVerificationOverride,
    FbsIntegrationProfile,
    FbsComplianceOverride,
    FbsControllerCheckTote,
    FbsControllerPickTote,
    FbsControllerToteOrder,
    FbsMarketplaceCommand,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderLabel,
    FbsPickRestockRequest,
    FbsWorkstation,
)
from fbs.order_audit import log_order_bulk_transition


WB_ACCEPTED_STATUSES = {"sorted"}
OZON_ACCEPTED_STATUSES = {"delivering", "delivered", "driver_pickup"}
MARKETPLACE_PROBLEM_STATUSES = {"cancel", "cancelled", "canceled", "rejected"}
WB_DESTINATION_PICKUP_POINT = "pickup_point"
WB_DESTINATION_WAREHOUSE = "warehouse_sc"


@dataclass(frozen=True)
class FbsHandoverManifestRow:
    box_qr: str
    external_box_id: str
    order_count: int
    status: str


@dataclass(frozen=True)
class FbsPackedOrderResult:
    label: FbsOrderLabel
    handover_order: FbsHandoverOrder
    box: FbsHandoverBox


@dataclass(frozen=True)
class FbsHandoverCompositionReadiness:
    ready: bool
    active_order_count: int
    blocked_order_count: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FbsHandoverArchiveReadiness:
    ready: bool
    reasons: tuple[str, ...]


def _require_writes() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Складские операции FBS выключены.")


def _authenticated_user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _normalized(value: str) -> str:
    return str(value or "").strip().casefold()


def handover_order_has_accepted_marketplace_status(
    order: FbsOrder,
    *,
    marketplace: str,
) -> bool:
    """Return whether the marketplace confirmed the specific handover order.

    Wildberries reports the final confirmation in ``wbStatus`` (stored as
    ``marketplace_substatus``), while its primary status stays ``complete``.
    Both fields are marketplace facts; neither one changes the order itself.
    """
    accepted_statuses = (
        WB_ACCEPTED_STATUSES
        if marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        else OZON_ACCEPTED_STATUSES
    )
    return bool(
        {
            _normalized(order.marketplace_status),
            _normalized(order.marketplace_substatus),
        }
        & accepted_statuses
    )


def _handover_verification_snapshot(batch: FbsHandoverBatch) -> dict:
    assignments = list(
        batch.order_assignments.exclude(
            status=FbsHandoverOrderAssignment.STATUS_CANCELED
        )
        .order_by("id")
        .values("id", "order_id", "status")
    )
    box_ids = list(batch.boxes.order_by("id").values_list("id", flat=True))
    box_orders = list(
        FbsHandoverOrder.objects.filter(
            box__batch=batch,
            status=FbsHandoverOrder.STATUS_ACTIVE,
        )
        .order_by("box_id", "order_id")
        .values("box_id", "order_id")
    )
    order_ids = [row["order_id"] for row in assignments]
    applied_labels = list(
        FbsOrderLabel.objects.filter(
            order_id__in=order_ids,
            status=FbsOrderLabel.STATUS_APPLIED,
        )
        .order_by("order_id", "id")
        .values("id", "order_id", "status")
    )
    required_metadata = list(
        FbsMarketplaceMetadataTransfer.objects.filter(
            order_item__order_id__in=order_ids,
        ).filter(
            Q(is_required=True)
            | Q(metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE)
        )
        .order_by("order_item__order_id", "order_item_id", "metadata_type", "id")
        .values(
            "id",
            "order_item__order_id",
            "order_item_id",
            "metadata_type",
            "status",
        )
    )
    return {
        "version": 2,
        "profile_id": batch.profile_id,
        "external_supply_id": str(batch.external_supply_id or ""),
        "assignments": assignments,
        "box_ids": box_ids,
        "box_orders": box_orders,
        "applied_labels": applied_labels,
        "required_metadata": required_metadata,
    }


def _active_handover_verification_override(
    batch: FbsHandoverBatch,
) -> FbsHandoverVerificationOverride | None:
    override = (
        FbsHandoverVerificationOverride.objects.filter(
            batch=batch,
            is_active=True,
        )
        .select_related("approved_by", "used_by")
        .first()
    )
    if override is None:
        return None
    if override.snapshot != _handover_verification_snapshot(batch):
        return None
    return override


def _has_handover_verification_override(batch: FbsHandoverBatch) -> bool:
    return _active_handover_verification_override(batch) is not None


def has_handover_verification_override(batch: FbsHandoverBatch) -> bool:
    """Return whether a still-valid manager verification override is active."""
    return _has_handover_verification_override(batch)


def handover_composition_readiness(
    batch: FbsHandoverBatch,
) -> FbsHandoverCompositionReadiness:
    """Return the authoritative gate for WB composition verification."""
    from .pick_restock import HANDOVER_BLOCKING_PICK_RESTOCK_STATUSES
    from .traceability import (
        marketplace_metadata_transfer_resolved,
        metadata_requirements,
    )

    assignments = list(
        batch.order_assignments.exclude(
            status=FbsHandoverOrderAssignment.STATUS_CANCELED
        )
        .select_related("order")
        .prefetch_related(
            "order__marketplace_labels",
            "order__items__metadata_transfers",
            "order__items__sku",
        )
        .order_by("id")
    )
    reasons: list[str] = []
    verification_override = _has_handover_verification_override(batch)
    blocked_order_ids: set[int] = set()
    blocking_returns = FbsPickRestockRequest.objects.filter(
        handover_assignment__batch=batch,
        status__in=HANDOVER_BLOCKING_PICK_RESTOCK_STATUSES,
    ).count()
    if blocking_returns:
        reasons.append(f"Не завершен возврат проблемных заказов: {blocking_returns}.")
    pending_supply_move_order_ids = set(FbsHandoverOrder.objects.filter(
        box__batch=batch,
        status=FbsHandoverOrder.STATUS_RETURN_PENDING,
    ).values_list("order_id", flat=True))
    pending_supply_move_order_ids.update(
        FbsHandoverOrderAssignment.objects.filter(
            status=FbsHandoverOrderAssignment.STATUS_PENDING,
            batch__compatibility_key__contains=f":source-{batch.id}:",
        ).values_list("order_id", flat=True)
    )
    pending_supply_moves = len(pending_supply_move_order_ids)
    if pending_supply_moves:
        reasons.append(
            "WB еще не подтвердил перенос проблемных заказов в другую поставку: "
            f"{pending_supply_moves}."
        )
    controller_check_tote = FbsControllerCheckTote.objects.filter(
        handover_batch=batch
    ).first()
    if (
        controller_check_tote is not None
        and controller_check_tote.status != FbsControllerCheckTote.STATUS_CLOSED
    ):
        active_pick_tote_count = controller_check_tote.pick_totes.filter(
            status__in=(
                FbsControllerPickTote.STATUS_PROCESSING,
                FbsControllerPickTote.STATUS_AWAITING_EMPTY,
            )
        ).count()
        if active_pick_tote_count:
            reasons.append(
                "Не завершены тары подбора контролера: "
                f"{active_pick_tote_count}."
            )
        elif not verification_override:
            reasons.append("Контролер еще не закрыл проверку состава отгрузки.")

    active_links: dict[int, FbsHandoverOrder] = {}
    for link_row in (
        FbsHandoverOrder.objects.filter(
            box__batch=batch,
            order_id__in=[assignment.order_id for assignment in assignments],
            status=FbsHandoverOrder.STATUS_ACTIVE,
        )
        .select_related("box", "verified_label")
        .order_by("id")
    ):
        active_links.setdefault(link_row.order_id, link_row)

    for assignment in assignments:
        order = assignment.order
        order_reasons = []
        if assignment.status != FbsHandoverOrderAssignment.STATUS_CONFIRMED:
            order_reasons.append("не подтвержден в поставке WB")
        if order.internal_status not in {
            FbsOrder.STATUS_READY_FOR_HANDOVER,
            FbsOrder.STATUS_HANDED_OVER,
            FbsOrder.STATUS_DELIVERED,
        }:
            order_reasons.append("не завершена проверка на рабочем столе")
        link = active_links.get(order.id)
        if link is None:
            order_reasons.append("заказ не добавлен в активный короб")
        elif not verification_override and (
            link.verified_at is None
            or link.verified_label_id is None
            or link.verified_label.status != FbsOrderLabel.STATUS_APPLIED
        ):
            order_reasons.append("этикетка заказа не отсканирована")

        for item in order.items.all():
            requirements = metadata_requirements(item)
            transfers = {
                transfer.metadata_type: transfer
                for transfer in item.metadata_transfers.all()
            }
            required_types = []
            if (
                requirements.marking_required
                or FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE in transfers
            ):
                required_types.append(FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE)
            if requirements.expiry_required:
                required_types.append(FbsMarketplaceMetadataTransfer.TYPE_EXPIRATION)
            for metadata_type in required_types:
                transfer = transfers.get(metadata_type)
                if not marketplace_metadata_transfer_resolved(transfer):
                    label = (
                        "КИЗ"
                        if metadata_type
                        == FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE
                        else "срок годности"
                    )
                    order_reasons.append(f"{label} не подтвержден")

        if order_reasons:
            blocked_order_ids.add(order.id)
            reasons.append(
                f"Заказ {order.external_order_id}: " + ", ".join(dict.fromkeys(order_reasons)) + "."
            )

    if not assignments:
        reasons.append("В отгрузке нет активных заказов.")
    return FbsHandoverCompositionReadiness(
        ready=bool(assignments) and not reasons,
        active_order_count=len(assignments),
        blocked_order_count=len(blocked_order_ids),
        reasons=tuple(reasons),
    )


def assert_handover_composition_ready(batch: FbsHandoverBatch) -> None:
    readiness = handover_composition_readiness(batch)
    if readiness.ready:
        return
    detail = readiness.reasons[0] if readiness.reasons else "Состав отгрузки не готов."
    raise FbsHandoverError(
        f"Проверка состава пока недоступна. Контрольный скан и готовность заказа "
        f"не подтверждены. {detail}"
    )


def wb_order_destination_kind(order: FbsOrder) -> str:
    raw_payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    delivery_type = _normalized(raw_payload.get("deliveryType"))
    if "pickup" in delivery_type or "pvz" in delivery_type:
        return WB_DESTINATION_PICKUP_POINT
    if raw_payload.get("scanPrice") is not None:
        return WB_DESTINATION_PICKUP_POINT
    return WB_DESTINATION_WAREHOUSE


def wb_handover_uses_marketplace_boxes(batch: FbsHandoverBatch) -> bool:
    compatibility_key = str(batch.compatibility_key or "")
    marker = ":destination:"
    if marker not in compatibility_key:
        # Legacy batches already in progress used WB transport boxes for every route.
        return True
    destination = compatibility_key.split(marker, 1)[1].split(":", 1)[0]
    return destination == WB_DESTINATION_PICKUP_POINT


def wb_handover_compatibility_key(
    order: FbsOrder,
    *,
    workstation_id: int | None = None,
    pick_batch_id: int | None = None,
    check_tote_id: int | None = None,
) -> str:
    requirements = [
        item.requirements if isinstance(item.requirements, dict) else {}
        for item in order.items.all()
    ]
    cargo_types = {
        str(item_requirements.get("cargo_type") or "").strip()
        for item_requirements in requirements
    }
    cargo_types.discard("")
    if len(cargo_types) > 1:
        raise FbsHandoverError("Заказ WB содержит разные cargoType.")
    cargo_type = next(iter(cargo_types), "unknown")
    b2b_values = {bool(item_requirements.get("is_b2b")) for item_requirements in requirements}
    if len(b2b_values) > 1:
        raise FbsHandoverError("Заказ WB содержит несовместимые B2B-позиции.")
    is_b2b = next(iter(b2b_values), False)
    raw_payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    address = raw_payload.get("address") if isinstance(raw_payload.get("address"), dict) else {}
    office_id = str(raw_payload.get("officeId") or address.get("officeId") or "unknown").strip()
    delivery_type = str(raw_payload.get("deliveryType") or "unknown").strip()
    destination = str(wb_order_destination_kind(order) or "").strip()
    key = (
        f"warehouse:{order.profile.external_warehouse_id}:cargo:{cargo_type}:"
        f"b2b:{int(is_b2b)}:office:{office_id}:delivery:{delivery_type}:"
        f"destination:{destination}"
    )
    destination_marker = ":destination:"
    destination_value = (
        key.split(destination_marker, 1)[1].split(":", 1)[0]
        if destination_marker in key
        else ""
    )
    if not destination_value:
        raise FbsHandoverError(
            "Ключ совместимости поставки WB не содержит обязательный destination."
        )
    if workstation_id:
        key = f"{key}:workstation:{int(workstation_id)}"
    if pick_batch_id:
        # A physical pick tote is the lifetime boundary of a WB supply.  Once
        # its controller flow is checked, later totes must open a new supply
        # instead of extending the one that is already being delivered.
        key = f"{key}:pick-batch:{int(pick_batch_id)}"
    if check_tote_id:
        key = f"{key}:check-tote:{int(check_tote_id)}"
    return key


def _ensure_wb_order_handover_assignment(
    *,
    order_id: int,
    assigned_by=None,
    workstation_id: int | None = None,
    pick_batch_id: int | None = None,
    check_tote_id: int | None = None,
    lock_nowait: bool,
) -> FbsHandoverOrderAssignment:
    _require_writes()
    order = (
        FbsOrder.objects.select_for_update(nowait=lock_nowait)
        .select_related("profile__agency")
        .prefetch_related("items")
        .get(pk=order_id)
    )
    if order.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        raise FbsHandoverError("Автоматическая поставка этого этапа поддерживает WB.")
    if order.internal_status != FbsOrder.STATUS_PICKED:
        raise FbsHandoverError("В поставку можно включить только проверяемый заказ.")
    check_tote = None
    if check_tote_id is not None:
        check_tote = (
            FbsControllerCheckTote.objects.select_for_update(
                nowait=lock_nowait
            )
            .filter(
                pk=check_tote_id,
                status__in=(
                    FbsControllerCheckTote.STATUS_OPEN,
                    FbsControllerCheckTote.STATUS_WAITING_KIZ,
                    FbsControllerCheckTote.STATUS_READY,
                    FbsControllerCheckTote.STATUS_COMPOSITION,
                ),
            )
            .first()
        )
        if check_tote is None:
            raise FbsHandoverError("Тара проверки закрыта или не найдена.")
        if check_tote.profile_id not in (None, order.profile_id):
            raise FbsHandoverError("Тара проверки относится к другому кабинету клиента.")
    existing_assignments = FbsHandoverOrderAssignment.objects.select_related(
        "batch"
    ).filter(order=order)
    if lock_nowait:
        existing_assignments = existing_assignments.select_for_update(
            nowait=True
        )
    existing = existing_assignments.first()
    if existing is not None:
        if check_tote is not None:
            if check_tote.handover_batch_id not in (None, existing.batch_id):
                raise FbsHandoverError("Заказ уже назначен в другую отгрузку.")
            if check_tote.handover_batch_id is None:
                check_tote.handover_batch = existing.batch
                check_tote.save(update_fields=["handover_batch", "updated_at"])
        return existing
    FbsIntegrationProfile.objects.select_for_update(nowait=lock_nowait).get(
        pk=order.profile_id
    )
    compatibility_key = wb_handover_compatibility_key(
        order,
        workstation_id=workstation_id,
        pick_batch_id=pick_batch_id,
        check_tote_id=check_tote_id,
    )
    batch = (
        FbsHandoverBatch.objects.select_for_update(nowait=lock_nowait)
        .filter(
            profile=order.profile,
            status=FbsHandoverBatch.STATUS_OPEN,
            compatibility_key=compatibility_key,
            marketplace_state__in=(
                FbsHandoverBatch.MARKETPLACE_DRAFT,
                FbsHandoverBatch.MARKETPLACE_CREATING,
                FbsHandoverBatch.MARKETPLACE_OPEN,
            ),
        )
        .order_by("created_at", "id")
        .first()
    )
    if batch is None:
        batch = FbsHandoverBatch.objects.create(
            profile=order.profile,
            external_name=f"FULLBOX-{order.profile_id}-{timezone.now():%Y%m%d-%H%M%S}-{order.id}",
            compatibility_key=compatibility_key,
            created_by=_authenticated_user(assigned_by),
        )
    elif batch.order_assignments.count() >= 1000:
        batch = FbsHandoverBatch.objects.create(
            profile=order.profile,
            external_name=f"FULLBOX-{order.profile_id}-{timezone.now():%Y%m%d-%H%M%S}-{order.id}",
            compatibility_key=compatibility_key,
            created_by=_authenticated_user(assigned_by),
        )
    assignment = FbsHandoverOrderAssignment.objects.create(
        batch=batch,
        order=order,
        assigned_by=_authenticated_user(assigned_by),
    )
    if check_tote is not None:
        if check_tote.handover_batch_id not in (None, batch.id):
            raise FbsHandoverError("Тара проверки уже связана с другой отгрузкой.")
        if check_tote.handover_batch_id is None:
            check_tote.handover_batch = batch
            check_tote.save(update_fields=["handover_batch", "updated_at"])
    if (
        feature_enabled("outbox")
        and order.profile.is_active
        and order.profile.outbox_enabled
    ):
        from .marketplace import schedule_wb_handover_order

        schedule_wb_handover_order(
            assignment_id=assignment.id,
            requested_by=_authenticated_user(assigned_by),
        )
    return assignment


@transaction.atomic
def ensure_wb_order_handover_assignment(
    *,
    order_id: int,
    assigned_by=None,
    workstation_id: int | None = None,
    pick_batch_id: int | None = None,
    check_tote_id: int | None = None,
) -> FbsHandoverOrderAssignment:
    return _ensure_wb_order_handover_assignment(
        order_id=order_id,
        assigned_by=assigned_by,
        workstation_id=workstation_id,
        pick_batch_id=pick_batch_id,
        check_tote_id=check_tote_id,
        lock_nowait=False,
    )


@transaction.atomic
def _prefetch_wb_order_handover_assignment(
    *,
    order_id: int,
    assigned_by=None,
    workstation_id: int | None = None,
    pick_batch_id: int | None = None,
    check_tote_id: int | None = None,
) -> FbsHandoverOrderAssignment:
    """Create the optional controller prefetch assignment without waiting."""
    return _ensure_wb_order_handover_assignment(
        order_id=order_id,
        assigned_by=assigned_by,
        workstation_id=workstation_id,
        pick_batch_id=pick_batch_id,
        check_tote_id=check_tote_id,
        lock_nowait=True,
    )


@transaction.atomic
def ensure_order_handover_assignment(
    *,
    order_id: int,
    assigned_by=None,
    workstation_id: int | None = None,
    pick_batch_id: int | None = None,
    check_tote_id: int | None = None,
) -> FbsHandoverOrderAssignment:
    """Create the client/marketplace shipment before the order label is confirmed."""
    _require_writes()
    order = (
        FbsOrder.objects.select_for_update()
        .select_related("profile__agency")
        .prefetch_related("items")
        .get(pk=order_id)
    )
    if order.internal_status != FbsOrder.STATUS_PICKED:
        raise FbsHandoverError("В отгрузку можно включить только проверяемый заказ.")
    check_tote = None
    if check_tote_id is not None:
        check_tote = FbsControllerCheckTote.objects.select_for_update().filter(
            pk=check_tote_id,
            status__in=(
                FbsControllerCheckTote.STATUS_OPEN,
                FbsControllerCheckTote.STATUS_WAITING_KIZ,
                FbsControllerCheckTote.STATUS_READY,
                FbsControllerCheckTote.STATUS_COMPOSITION,
            ),
        ).first()
        if check_tote is None:
            raise FbsHandoverError("Тара проверки закрыта или не найдена.")
        if check_tote.profile_id not in (None, order.profile_id):
            raise FbsHandoverError("Тара проверки относится к другому кабинету клиента.")
    existing = FbsHandoverOrderAssignment.objects.select_related("batch").filter(
        order=order
    ).first()
    if existing is not None:
        if check_tote is not None:
            if check_tote.handover_batch_id not in (None, existing.batch_id):
                raise FbsHandoverError("Заказ уже назначен в другую отгрузку.")
            if check_tote.handover_batch_id is None:
                check_tote.handover_batch = existing.batch
                check_tote.save(update_fields=["handover_batch", "updated_at"])
        return existing
    if order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        return ensure_wb_order_handover_assignment(
            order_id=order.id,
            assigned_by=assigned_by,
            workstation_id=workstation_id,
            pick_batch_id=pick_batch_id,
            check_tote_id=check_tote_id,
        )

    FbsIntegrationProfile.objects.select_for_update().get(pk=order.profile_id)
    compatibility_key = (
        f"warehouse:{order.profile.external_warehouse_id}:marketplace:"
        f"{order.profile.marketplace}:workstation:{int(workstation_id or 0)}"
    )
    if check_tote_id:
        compatibility_key = f"{compatibility_key}:check-tote:{int(check_tote_id)}"
    batch = (
        FbsHandoverBatch.objects.select_for_update()
        .filter(
            profile=order.profile,
            status=FbsHandoverBatch.STATUS_OPEN,
            compatibility_key=compatibility_key,
        )
        .order_by("created_at", "id")
        .first()
    )
    if batch is None or batch.order_assignments.count() >= 1000:
        batch = FbsHandoverBatch.objects.create(
            profile=order.profile,
            external_name=(
                f"FULLBOX-{order.profile_id}-{timezone.now():%Y%m%d-%H%M%S}-{order.id}"
            ),
            compatibility_key=compatibility_key,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
            created_by=_authenticated_user(assigned_by),
        )
    assignment = FbsHandoverOrderAssignment.objects.create(
        batch=batch,
        order=order,
        status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        assigned_by=_authenticated_user(assigned_by),
        confirmed_at=timezone.now(),
    )
    if check_tote is not None:
        if check_tote.handover_batch_id not in (None, batch.id):
            raise FbsHandoverError("Тара проверки уже связана с другой отгрузкой.")
        if check_tote.handover_batch_id is None:
            check_tote.handover_batch = batch
            check_tote.save(update_fields=["handover_batch", "updated_at"])
    if not batch.boxes.filter(status=FbsHandoverBox.STATUS_OPEN).exists():
        suffix = uuid4().hex[:12].upper()
        FbsHandoverBox.objects.create(
            batch=batch,
            qr_code=f"FBS-{order.profile.marketplace.upper()}-BOX-{batch.id}-{suffix}",
            external_box_id=f"FULLBOX-{batch.id}-{suffix}",
        )
    return assignment


def handover_archive_readiness(
    batch: FbsHandoverBatch,
) -> FbsHandoverArchiveReadiness:
    from .pick_restock import BLOCKING_PICK_RESTOCK_STATUSES

    reasons = []
    if batch.status == FbsHandoverBatch.STATUS_ARCHIVED:
        reasons.append("Отгрузка уже находится в архиве.")
    if batch.marketplace_state == FbsHandoverBatch.MARKETPLACE_DELIVERY_PENDING:
        reasons.append("Отгрузка передается marketplace.")
    if batch.order_assignments.filter(
        status__in=(
            FbsHandoverOrderAssignment.STATUS_PENDING,
            FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
    ).exists():
        reasons.append("В отгрузке есть действующие заказы.")
    if FbsHandoverOrder.objects.filter(
        box__batch=batch,
        status__in=(
            FbsHandoverOrder.STATUS_ACTIVE,
            FbsHandoverOrder.STATUS_RETURN_PENDING,
        ),
    ).exists():
        reasons.append("В коробах остался действующий заказ или незавершенный возврат.")
    if FbsPickRestockRequest.objects.filter(
        handover_assignment__batch=batch,
        status__in=BLOCKING_PICK_RESTOCK_STATUSES,
    ).exists():
        reasons.append("По отгрузке еще не завершен возврат отбора.")
    if batch.marketplace_commands.filter(
        status__in=(
            FbsMarketplaceCommand.STATUS_PENDING,
            FbsMarketplaceCommand.STATUS_SENT,
            FbsMarketplaceCommand.STATUS_RETRY,
        )
    ).exists():
        reasons.append("По отгрузке еще выполняется команда marketplace.")
    return FbsHandoverArchiveReadiness(
        ready=not reasons,
        reasons=tuple(reasons),
    )


@transaction.atomic
def archive_handover_batch(
    *,
    batch_id: int,
    archived_by,
    reason: str,
) -> FbsHandoverBatch:
    """Hide a terminal empty shipment without deleting its marketplace audit trail."""
    _require_writes()
    actor = _authenticated_user(archived_by)
    if actor is None:
        raise FbsHandoverError("Не указан сотрудник, архивирующий отгрузку.")
    reason = str(reason or "").strip()
    if not reason:
        raise FbsHandoverError("Укажите причину архивирования отгрузки.")

    batch = FbsHandoverBatch.objects.select_for_update().get(pk=batch_id)
    if batch.status == FbsHandoverBatch.STATUS_ARCHIVED:
        return batch
    readiness = handover_archive_readiness(batch)
    if not readiness.ready:
        raise FbsHandoverError(readiness.reasons[0])

    batch.status = FbsHandoverBatch.STATUS_ARCHIVED
    batch.archived_by = actor
    batch.archived_at = timezone.now()
    batch.archive_reason = reason
    batch.save(
        update_fields=(
            "status",
            "archived_by",
            "archived_at",
            "archive_reason",
            "updated_at",
        )
    )
    return batch


@transaction.atomic
def bind_active_handover_box(
    *, workstation_id: int, box_qr_scan: str, performed_by
) -> FbsHandoverBox:
    _require_writes()
    actor = _authenticated_user(performed_by)
    if actor is None:
        raise FbsHandoverError("Не указан контролер, активирующий короб.")
    scan = str(box_qr_scan or "").strip()
    if not scan:
        raise FbsHandoverError("Отсканируйте QR транспортного короба.")
    box = (
        FbsHandoverBox.objects.select_for_update()
        .select_related("batch__profile__agency")
        .filter(qr_code=scan)
        .first()
    )
    if box is None or box.qr_code.startswith("PENDING:"):
        raise FbsHandoverError("QR готового транспортного короба FBS не найден.")
    if box.status != FbsHandoverBox.STATUS_OPEN:
        raise FbsHandoverError("Короб уже закрыт или передан.")
    if box.batch.status != FbsHandoverBatch.STATUS_OPEN:
        raise FbsHandoverError("Отгрузка этого короба уже закрыта.")
    if (
        box.batch.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        and wb_handover_uses_marketplace_boxes(box.batch)
        and (not box.external_box_id or not box.label_file)
    ):
        raise FbsHandoverError("WB еще не передал готовый QR транспортного короба.")
    workstation = (
        FbsWorkstation.objects.select_for_update(of=("self",))
        .select_related("active_handover_box")
        .get(pk=workstation_id, is_active=True)
    )
    occupied = FbsWorkstation.objects.select_for_update().filter(
        active_handover_box=box
    ).exclude(pk=workstation.pk).first()
    if occupied is not None:
        raise FbsHandoverError(f"Короб уже активен на рабочем месте {occupied.name}.")
    workstation.active_handover_box = box
    workstation.updated_by = actor
    workstation.save(update_fields=["active_handover_box", "updated_by", "updated_at"])
    return box


@transaction.atomic
def clear_active_handover_box(*, workstation_id: int, performed_by) -> None:
    _require_writes()
    actor = _authenticated_user(performed_by)
    if actor is None:
        raise FbsHandoverError("Не указан контролер рабочего места.")
    workstation = FbsWorkstation.objects.select_for_update().get(pk=workstation_id)
    if workstation.active_handover_box_id is None:
        return
    workstation.active_handover_box = None
    workstation.updated_by = actor
    workstation.save(update_fields=["active_handover_box", "updated_by", "updated_at"])


def _ready_open_handover_boxes(assignment: FbsHandoverOrderAssignment):
    boxes = FbsHandoverBox.objects.filter(
        batch_id=assignment.batch_id,
        status=FbsHandoverBox.STATUS_OPEN,
        batch__status=FbsHandoverBatch.STATUS_OPEN,
    ).exclude(qr_code__startswith="PENDING:")
    if assignment.batch.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        if wb_handover_uses_marketplace_boxes(assignment.batch):
            boxes = boxes.exclude(external_box_id="").exclude(label_file="")
    return boxes.order_by("id")


def get_ready_handover_box_for_order(*, order_id: int) -> FbsHandoverBox | None:
    """Return the ready box scoped to the order's confirmed marketplace shipment."""
    assignment = (
        FbsHandoverOrderAssignment.objects.select_related("batch__profile")
        .filter(
            order_id=order_id,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        .first()
    )
    if assignment is None:
        return None
    return _ready_open_handover_boxes(assignment).first()


@transaction.atomic
def ensure_active_handover_box_for_order(
    *, workstation_id: int, order_id: int, performed_by
) -> FbsHandoverBox:
    """Bind the order's ready shipment box to the workstation without a box scan."""
    _require_writes()
    actor = _authenticated_user(performed_by)
    if actor is None:
        raise FbsHandoverError("Не указан контролер, закрывающий заказ.")
    assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update()
        .select_related("batch__profile")
        .filter(order_id=order_id)
        .first()
    )
    if assignment is None:
        raise FbsHandoverError("Отгрузка заказа еще создается. Повторите сканирование позже.")
    if assignment.status != FbsHandoverOrderAssignment.STATUS_CONFIRMED:
        detail = str(assignment.error or "").strip()
        if detail:
            raise FbsHandoverError(f"Маркетплейс не подтвердил отгрузку: {detail}")
        raise FbsHandoverError(
            "Маркетплейс еще подтверждает заказ в отгрузке. Повторите сканирование позже."
        )
    if assignment.batch.status != FbsHandoverBatch.STATUS_OPEN:
        raise FbsHandoverError("Отгрузка этого заказа уже закрыта.")
    box = _ready_open_handover_boxes(assignment).select_for_update().first()
    if box is None:
        raise FbsHandoverError(
            "Транспортный короб marketplace еще создается. Повторите сканирование позже."
        )
    workstation = (
        FbsWorkstation.objects.select_for_update(of=("self",))
        .get(pk=workstation_id, is_active=True)
    )
    occupied = (
        FbsWorkstation.objects.select_for_update()
        .filter(active_handover_box_id=box.id)
        .exclude(pk=workstation.pk)
        .first()
    )
    if occupied is not None:
        raise FbsHandoverError(f"Короб уже используется на рабочем месте {occupied.name}.")
    if workstation.active_handover_box_id != box.id:
        workstation.active_handover_box = box
        workstation.updated_by = actor
        workstation.save(
            update_fields=["active_handover_box", "updated_by", "updated_at"]
        )
    return box


@transaction.atomic
def confirm_order_label_and_pack(
    *, label_id: int, label_scan: str, workstation_id: int, performed_by
) -> FbsPackedOrderResult:
    """Confirm the order label and place it into the workstation box atomically."""
    _require_writes()
    actor = _authenticated_user(performed_by)
    if actor is None:
        raise FbsHandoverError("Не указан контролер, закрывающий заказ.")
    order_id = (
        FbsOrderLabel.objects.filter(pk=label_id)
        .values_list("order_id", flat=True)
        .first()
    )
    if order_id is None:
        raise FbsHandoverError("Этикетка заказа не найдена.")
    box = ensure_active_handover_box_for_order(
        workstation_id=workstation_id,
        order_id=order_id,
        performed_by=actor,
    )

    from .labels import confirm_order_label_scan

    label = confirm_order_label_scan(
        label_id=label_id,
        label_scan=label_scan,
        performed_by=actor,
    )
    link = add_order_to_handover_box(
        box_id=box.id,
        order_label_scan=label_scan,
        added_by=actor,
    )
    return FbsPackedOrderResult(label=label, handover_order=link, box=box)


@transaction.atomic
def create_handover_batch(
    *,
    profile: FbsIntegrationProfile,
    external_supply_id: str = "",
    created_by=None,
) -> FbsHandoverBatch:
    _require_writes()
    profile = FbsIntegrationProfile.objects.select_for_update().get(pk=profile.pk)
    external_supply_id = str(external_supply_id or "").strip()
    existing = None
    if external_supply_id:
        existing = FbsHandoverBatch.objects.filter(
            profile=profile,
            external_supply_id=external_supply_id,
            status__in=(
                FbsHandoverBatch.STATUS_OPEN,
                FbsHandoverBatch.STATUS_READY,
                FbsHandoverBatch.STATUS_DISPATCHED,
            ),
        ).first()
    if existing is not None:
        return existing
    batch = FbsHandoverBatch.objects.create(
        profile=profile,
        external_supply_id=external_supply_id,
        external_name=f"FULLBOX-{profile.id}-{timezone.now():%Y%m%d-%H%M%S}",
        marketplace_state=(
            FbsHandoverBatch.MARKETPLACE_OPEN
            if external_supply_id
            else FbsHandoverBatch.MARKETPLACE_DRAFT
        ),
        created_by=_authenticated_user(created_by),
    )
    return batch


@transaction.atomic
def add_handover_box(
    *,
    batch_id: int,
    qr_code: str,
    external_box_id: str = "",
) -> FbsHandoverBox:
    _require_writes()
    qr_code = str(qr_code or "").strip()
    if not qr_code:
        raise FbsHandoverError("Маркетплейс не передал QR короба.")
    batch = FbsHandoverBatch.objects.select_for_update().get(pk=batch_id)
    if batch.status != FbsHandoverBatch.STATUS_OPEN:
        raise FbsHandoverError("В поставку больше нельзя добавлять короба.")
    existing = FbsHandoverBox.objects.filter(qr_code=qr_code).first()
    if existing is not None:
        if existing.batch_id == batch.id:
            return existing
        raise FbsHandoverError("Этот QR уже относится к другой поставке.")
    return FbsHandoverBox.objects.create(
        batch=batch,
        qr_code=qr_code,
        external_box_id=str(external_box_id or "").strip(),
    )


@transaction.atomic
def add_wb_handover_boxes(
    *, batch_id: int, amount: int, requested_by=None
):
    _require_writes()
    batch = FbsHandoverBatch.objects.select_for_update().get(pk=batch_id)
    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        raise FbsHandoverError("Автоматическое создание коробов доступно для WB.")
    if not wb_handover_uses_marketplace_boxes(batch):
        raise FbsHandoverError(
            "Короба WB-MP создаются только для поставок в ПВЗ. "
            "Для склада или СЦ используется QR поставки WB-GI."
        )
    if batch.order_assignments.exclude(
        status=FbsHandoverOrderAssignment.STATUS_CANCELED
    ).exclude(
        status=FbsHandoverOrderAssignment.STATUS_CONFIRMED
    ).exists():
        raise FbsHandoverError("Не все заказы подтверждены в поставке WB.")
    if batch.order_assignments.exclude(
        status=FbsHandoverOrderAssignment.STATUS_CANCELED
    ).exclude(
        order__internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER
    ).exists():
        raise FbsHandoverError("Не все заказы проверены и закрыты QR-этикеткой.")
    from .marketplace import schedule_wb_handover_boxes

    return schedule_wb_handover_boxes(
        batch_id=batch.id,
        amount=int(amount),
        requested_by=_authenticated_user(requested_by),
    )


def _required_metadata_confirmed(
    order: FbsOrder,
    *,
    handover_batch: FbsHandoverBatch | None = None,
) -> bool:
    from .traceability import marketplace_metadata_transfer_resolved

    transfers = FbsMarketplaceMetadataTransfer.objects.filter(
        order_item__order=order,
    )
    if handover_batch is not None:
        current_pick_task_ids = list(
            FbsControllerToteOrder.objects.filter(
                order=order,
                check_tote__handover_batch=handover_batch,
            )
            .exclude(status=FbsControllerToteOrder.STATUS_REMOVED)
            .values_list("pick_tote__pick_batch__tasks__id", flat=True)
            .distinct()
        )
        if current_pick_task_ids:
            transfers = transfers.filter(
                Q(traceability__isnull=True)
                | Q(
                    traceability__allocation__pick_task_id__in=(
                        current_pick_task_ids
                    )
                )
            )
    unresolved_types = {
        transfer.metadata_type
        for transfer in transfers.filter(
            Q(is_required=True)
            | Q(metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE)
        ).select_related("order_item__order__profile")
        if not marketplace_metadata_transfer_resolved(transfer)
    }
    if not unresolved_types:
        return True
    overridden_types = set(
        FbsComplianceOverride.objects.filter(
            order=order,
            is_active=True,
            metadata_type__in=unresolved_types,
        ).values_list("metadata_type", flat=True)
    )
    return unresolved_types.issubset(overridden_types)


def _require_handover_override_actor(user):
    actor = _authenticated_user(user)
    if actor is None:
        raise FbsHandoverError("Не указан начальник склада, разрешающий отгрузку.")
    from employees.access import get_employee_roles
    from employees.models import Employee

    allowed_roles = {"head_manager", "director", "admin"}
    employees = Employee.objects.filter(user=actor, is_active=True)
    if not actor.is_superuser and not any(
        allowed_roles.intersection(get_employee_roles(employee))
        for employee in employees
    ):
        raise FbsHandoverError(
            "Аварийная отгрузка доступна только начальнику склада."
        )
    return actor


def _assert_required_metadata_actually_confirmed(order: FbsOrder) -> None:
    from .traceability import (
        marketplace_metadata_transfer_resolved,
        metadata_requirements,
    )

    for item in order.items.all():
        requirements = metadata_requirements(item)
        transfers = {
            transfer.metadata_type: transfer
            for transfer in item.metadata_transfers.all()
        }
        required_types = []
        if (
            requirements.marking_required
            or FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE in transfers
        ):
            required_types.append(FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE)
        if requirements.expiry_required:
            required_types.append(FbsMarketplaceMetadataTransfer.TYPE_EXPIRATION)
        for metadata_type in required_types:
            transfer = transfers.get(metadata_type)
            if not marketplace_metadata_transfer_resolved(transfer):
                label = (
                    "КИЗ"
                    if metadata_type
                    == FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE
                    else "срок годности"
                )
                raise FbsHandoverError(
                    f"Заказ {order.external_order_id}: {label} не подтвержден "
                    "маркетплейсом. Аварийная отгрузка это требование не отменяет."
                )


def _handover_primary_scan_link_plan(
    batch: FbsHandoverBatch,
    *,
    lock: bool = False,
) -> tuple[FbsHandoverBox, tuple[FbsControllerToteOrder, ...]] | None:
    """Return an exact link plan based only on completed primary label scans."""
    compatibility_key = str(batch.compatibility_key or "")
    if ":destination:warehouse_sc" not in compatibility_key:
        return None

    assignments_qs = batch.order_assignments.exclude(
        status=FbsHandoverOrderAssignment.STATUS_CANCELED
    )
    if lock:
        assignments_qs = assignments_qs.select_for_update()
    assignment_order_ids = set(
        assignments_qs.values_list("order_id", flat=True)
    )
    if not assignment_order_ids:
        return None

    existing_links = FbsHandoverOrder.objects.filter(
        order_id__in=assignment_order_ids
    )
    if lock:
        existing_links = existing_links.select_for_update()
    if existing_links.exists():
        return None

    boxes_qs = batch.boxes.order_by("id")
    if lock:
        boxes_qs = boxes_qs.select_for_update()
    boxes = list(boxes_qs)
    if len(boxes) != 1:
        raise FbsHandoverError(
            "Для передачи по первичной проверке нужен ровно один транспортный короб Fullbox."
        )
    box = boxes[0]
    if box.status != FbsHandoverBox.STATUS_OPEN or box.orders.exists():
        raise FbsHandoverError(
            f"Короб {box.qr_code} уже содержит состав или недоступен для подтверждения."
        )

    tote_orders_qs = (
        FbsControllerToteOrder.objects.filter(
            check_tote__handover_batch=batch,
            status=FbsControllerToteOrder.STATUS_LABELED,
        )
        .select_related("order", "label")
        .order_by("id")
    )
    if lock:
        tote_orders_qs = tote_orders_qs.select_for_update()
    tote_orders = tuple(tote_orders_qs)
    scanned_order_ids = {row.order_id for row in tote_orders}
    if (
        len(tote_orders) != len(assignment_order_ids)
        or scanned_order_ids != assignment_order_ids
    ):
        raise FbsHandoverError(
            "Первичная проверка не подтверждает точный состав активных заказов. "
            "Повторная проверка обязательна."
        )
    for row in tote_orders:
        if row.transport_box_id is not None:
            raise FbsHandoverError(
                f"Заказ {row.order.external_order_id} уже связан с другим коробом."
            )
        if (
            row.label_id is None
            or row.label.order_id != row.order_id
            or row.label.status != FbsOrderLabel.STATUS_APPLIED
            or row.label_confirmed_at is None
            or row.label_confirmed_by_id is None
        ):
            raise FbsHandoverError(
                f"Первичный скан заказа {row.order.external_order_id} не подтвержден."
            )
    return box, tote_orders


def _assert_handover_verification_override_preconditions(
    batch: FbsHandoverBatch,
    *,
    allow_primary_scan_link_plan: bool = False,
    lock_primary_scan_rows: bool = False,
) -> tuple[FbsHandoverBox, tuple[FbsControllerToteOrder, ...]] | None:
    from .pick_restock import HANDOVER_BLOCKING_PICK_RESTOCK_STATUSES

    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        raise FbsHandoverError(
            "Аварийный пропуск повторной проверки доступен только для WB."
        )
    if batch.status not in {
        FbsHandoverBatch.STATUS_OPEN,
        FbsHandoverBatch.STATUS_READY,
    }:
        raise FbsHandoverError("Отгрузка уже закрыта или недоступна для проверки.")
    if not str(batch.external_supply_id or "").strip():
        raise FbsHandoverError("У отгрузки нет подтвержденного номера поставки WB.")
    if batch.marketplace_state not in {
        FbsHandoverBatch.MARKETPLACE_OPEN,
        FbsHandoverBatch.MARKETPLACE_DELIVERY_PENDING,
        FbsHandoverBatch.MARKETPLACE_COMPLETE,
    }:
        raise FbsHandoverError(
            "Поставка WB не открыта или находится в ошибочном статусе."
        )
    if FbsPickRestockRequest.objects.filter(
        handover_assignment__batch=batch,
        status__in=HANDOVER_BLOCKING_PICK_RESTOCK_STATUSES,
    ).exists():
        raise FbsHandoverError(
            "Есть незавершенный возврат проблемного заказа. "
            "Аварийный пропуск повторной проверки его не отменяет."
        )
    if FbsControllerPickTote.objects.filter(
        check_tote__handover_batch=batch,
        status__in=(
            FbsControllerPickTote.STATUS_PROCESSING,
            FbsControllerPickTote.STATUS_AWAITING_EMPTY,
        ),
    ).exists():
        raise FbsHandoverError(
            "Есть незавершенная тара подбора контролера. Сначала завершите ее обработку."
        )

    primary_scan_link_plan = None
    if allow_primary_scan_link_plan:
        primary_scan_link_plan = _handover_primary_scan_link_plan(
            batch,
            lock=lock_primary_scan_rows,
        )
    planned_order_ids = (
        {row.order_id for row in primary_scan_link_plan[1]}
        if primary_scan_link_plan is not None
        else set()
    )

    assignments = list(
        batch.order_assignments.exclude(
            status=FbsHandoverOrderAssignment.STATUS_CANCELED
        )
        .select_related("order")
        .prefetch_related(
            "order__marketplace_labels",
            "order__items__metadata_transfers",
        )
        .order_by("id")
    )
    if not assignments:
        raise FbsHandoverError("В отгрузке нет активных заказов.")
    for assignment in assignments:
        order = assignment.order
        if assignment.status != FbsHandoverOrderAssignment.STATUS_CONFIRMED:
            raise FbsHandoverError(
                f"Заказ {order.external_order_id} не подтвержден в поставке WB."
            )
        if order.internal_status not in {
            FbsOrder.STATUS_READY_FOR_HANDOVER,
            FbsOrder.STATUS_HANDED_OVER,
            FbsOrder.STATUS_DELIVERED,
        }:
            raise FbsHandoverError(
                f"Заказ {order.external_order_id} не завершил проверку товара."
            )
        has_active_link = FbsHandoverOrder.objects.filter(
            order=order,
            box__batch=batch,
            status=FbsHandoverOrder.STATUS_ACTIVE,
        ).exists()
        if not has_active_link and order.id not in planned_order_ids:
            raise FbsHandoverError(
                f"Заказ {order.external_order_id} не добавлен в активный короб."
            )
        if not any(
            label.status == FbsOrderLabel.STATUS_APPLIED
            for label in order.marketplace_labels.all()
        ):
            raise FbsHandoverError(
                f"Для заказа {order.external_order_id} нет примененной этикетки WB."
            )
        _assert_required_metadata_actually_confirmed(order)

    boxes = list(batch.boxes.order_by("id"))
    if not boxes:
        raise FbsHandoverError("В отгрузке WB нет транспортного короба.")
    allowed_box_statuses = {
        FbsHandoverBox.STATUS_OPEN,
        FbsHandoverBox.STATUS_CLOSED,
        FbsHandoverBox.STATUS_SCANNED,
    }
    marketplace_boxes = wb_handover_uses_marketplace_boxes(batch)
    for box in boxes:
        is_planned_box = (
            primary_scan_link_plan is not None
            and box.id == primary_scan_link_plan[0].id
        )
        if (
            not box.orders.filter(status=FbsHandoverOrder.STATUS_ACTIVE).exists()
            and not is_planned_box
        ):
            raise FbsHandoverError(
                f"Пустой короб {box.qr_code} блокирует аварийную отгрузку."
            )
        if box.status not in allowed_box_statuses:
            raise FbsHandoverError(
                f"Короб {box.qr_code} недоступен в текущем статусе."
            )
        if marketplace_boxes and not box.label_file:
            raise FbsHandoverError(
                f"WB еще не передал QR транспортного короба {box.qr_code}."
            )
    return primary_scan_link_plan


def handover_verification_override_status(
    batch: FbsHandoverBatch,
) -> tuple[FbsHandoverVerificationOverride | None, str]:
    """Return the active override or the exact reason why it cannot be issued."""
    active_override = _active_handover_verification_override(batch)
    if active_override is not None:
        return active_override, ""
    try:
        _assert_handover_verification_override_preconditions(
            batch,
            allow_primary_scan_link_plan=True,
        )
    except FbsHandoverError as exc:
        return None, str(exc)
    return None, ""


@transaction.atomic
def approve_handover_verification_override(
    *,
    batch_id: int,
    reason: str,
    approved_by,
) -> FbsHandoverVerificationOverride:
    _require_writes()
    actor = _require_handover_override_actor(approved_by)
    reason = str(reason or "").strip()
    if not reason:
        raise FbsHandoverError("Укажите причину аварийной отгрузки.")
    batch = (
        FbsHandoverBatch.objects.select_for_update()
        .select_related("profile")
        .get(pk=batch_id)
    )
    primary_scan_link_plan = _assert_handover_verification_override_preconditions(
        batch,
        allow_primary_scan_link_plan=True,
        lock_primary_scan_rows=True,
    )
    if primary_scan_link_plan is not None:
        box, tote_orders = primary_scan_link_plan
        for tote_order in tote_orders:
            link = FbsHandoverOrder(
                box=box,
                order=tote_order.order,
                added_by=actor,
            )
            link.full_clean()
            link.save()
        _assert_handover_verification_override_preconditions(batch)
    snapshot = _handover_verification_snapshot(batch)
    existing = FbsHandoverVerificationOverride.objects.select_for_update().filter(
        batch=batch,
        is_active=True,
    ).first()
    if existing is not None and existing.snapshot == snapshot:
        return existing
    if existing is not None:
        existing.is_active = False
        existing.revoked_at = timezone.now()
        existing.save(update_fields=["is_active", "revoked_at"])
    override = FbsHandoverVerificationOverride(
        batch=batch,
        reason=reason,
        snapshot=snapshot,
        approved_by=actor,
    )
    override.full_clean()
    override.save()
    return override


def _mark_handover_verification_override_used(
    batch: FbsHandoverBatch,
    *,
    used_by,
) -> None:
    override = _active_handover_verification_override(batch)
    if override is None or override.used_at is not None:
        return
    override.used_by = _authenticated_user(used_by)
    override.used_at = timezone.now()
    override.save(update_fields=["used_by", "used_at"])


def assert_wb_handover_orders_verified(batch: FbsHandoverBatch) -> None:
    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        return
    if _has_handover_verification_override(batch):
        return
    expected_order_ids = set(
        batch.order_assignments.filter(
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        ).values_list("order_id", flat=True)
    )
    verified_order_ids = set(
        FbsHandoverOrder.objects.filter(
            box__batch=batch,
            status=FbsHandoverOrder.STATUS_ACTIVE,
            verified_at__isnull=False,
        ).values_list("order_id", flat=True)
    )
    missing_count = len(expected_order_ids - verified_order_ids)
    if not expected_order_ids:
        raise FbsHandoverError("В поставке WB нет подтвержденных заказов.")
    if missing_count:
        raise FbsHandoverError(
            f"Контрольный скан не выполнен для {missing_count} заказов WB."
        )


def _assert_wb_box_orders_verified(box: FbsHandoverBox) -> None:
    if box.batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        return
    if _has_handover_verification_override(box.batch):
        return
    missing_count = box.orders.filter(
        status=FbsHandoverOrder.STATUS_ACTIVE,
        verified_at__isnull=True,
    ).count()
    if missing_count:
        raise FbsHandoverError(
            f"Контрольный скан не выполнен для {missing_count} заказов в коробе."
        )


@transaction.atomic
def approve_compliance_override(
    *,
    order_id: int,
    metadata_type: str,
    reason: str,
    approved_by,
) -> FbsComplianceOverride:
    _require_writes()
    actor = _authenticated_user(approved_by)
    if actor is None:
        raise FbsHandoverError("Не указан руководитель, разрешающий отгрузку.")
    from employees.models import Employee

    if not actor.is_superuser and not Employee.objects.filter(
        user=actor,
        is_active=True,
        role__in=("head_manager", "director", "admin"),
    ).exists():
        raise FbsHandoverError("Разрешение отгрузки доступно только начальнику склада.")
    if metadata_type not in dict(FbsMarketplaceMetadataTransfer.TYPE_CHOICES):
        raise FbsHandoverError("Неизвестный тип обязательных данных.")
    reason = str(reason or "").strip()
    if not reason:
        raise FbsHandoverError("Укажите причину разрешения отгрузки.")
    order = FbsOrder.objects.select_for_update().get(pk=order_id)
    overrideable_statuses = (
        FbsMarketplaceMetadataTransfer.STATUS_FAILED,
        FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
        FbsMarketplaceMetadataTransfer.STATUS_UNSUPPORTED,
    )
    if not FbsMarketplaceMetadataTransfer.objects.filter(
        order_item__order=order,
        metadata_type=metadata_type,
        is_required=True,
        status__in=overrideable_statuses,
    ).exists():
        raise FbsHandoverError(
            "Разрешение возможно только для обязательных данных с подтвержденной ошибкой."
        )
    existing = FbsComplianceOverride.objects.filter(
        order=order,
        metadata_type=metadata_type,
        is_active=True,
    ).first()
    if existing is not None:
        return existing
    override = FbsComplianceOverride(
        order=order,
        metadata_type=metadata_type,
        reason=reason,
        approved_by=actor,
    )
    override.full_clean()
    override.save()
    from .picking import refresh_pick_batch_verification

    for batch_id in order.pick_tasks.values_list("batch_id", flat=True):
        refresh_pick_batch_verification(batch_id=batch_id)
    return override


@transaction.atomic
def add_order_to_handover_box(
    *,
    box_id: int,
    order_label_scan: str,
    added_by,
) -> FbsHandoverOrder:
    _require_writes()
    actor = _authenticated_user(added_by)
    if actor is None:
        raise FbsHandoverError("Не указан сотрудник, сканирующий заказ.")
    box = (
        FbsHandoverBox.objects.select_for_update(of=("self",))
        .select_related("batch__profile")
        .get(pk=box_id)
    )
    if box.status != FbsHandoverBox.STATUS_OPEN:
        raise FbsHandoverError("Короб уже закрыт.")
    label = (
        FbsOrderLabel.objects.select_for_update(of=("self",))
        .select_related("order")
        .filter(barcode=str(order_label_scan or "").strip())
        .order_by("-id")
        .first()
    )
    if label is None:
        raise FbsHandoverError("Этикетка заказа не найдена.")
    order = FbsOrder.objects.select_for_update().get(pk=label.order_id)
    preconfirmed_ozon_composition = False
    if label.status != FbsOrderLabel.STATUS_APPLIED:
        from .labels import is_preconfirmed_ozon_order_label

        preconfirmed_ozon_composition = bool(
            box.batch.profile.marketplace
            == FbsIntegrationProfile.MARKETPLACE_OZON
            and order.profile_id == box.batch.profile_id
            and order.internal_status == FbsOrder.STATUS_PICKED
            and is_preconfirmed_ozon_order_label(label)
            and FbsHandoverOrderAssignment.objects.filter(
                batch=box.batch,
                order=order,
                status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            ).exists()
            and FbsControllerToteOrder.objects.filter(
                check_tote__handover_batch=box.batch,
                order=order,
                label=label,
                status__in=(
                    FbsControllerToteOrder.STATUS_LABELED,
                    FbsControllerToteOrder.STATUS_COMPOSITION,
                ),
            ).exists()
        )
    if label.status != FbsOrderLabel.STATUS_APPLIED and not preconfirmed_ozon_composition:
        raise FbsHandoverError("Этикетка заказа еще не подтверждена сборщиком.")
    if (
        order.internal_status != FbsOrder.STATUS_READY_FOR_HANDOVER
        and not preconfirmed_ozon_composition
    ):
        raise FbsHandoverError("Заказ еще не готов к передаче.")
    if order.profile_id != box.batch.profile_id:
        raise FbsHandoverError("Заказ относится к другому кабинету маркетплейса.")
    if box.batch.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        assignment = FbsHandoverOrderAssignment.objects.filter(
            batch=box.batch,
            order=order,
        ).first()
        if assignment is None or assignment.status != FbsHandoverOrderAssignment.STATUS_CONFIRMED:
            raise FbsHandoverError("Заказ не подтвержден в этой поставке WB.")
    existing = FbsHandoverOrder.objects.filter(order=order).first()
    if existing is not None:
        if existing.status == FbsHandoverOrder.STATUS_EXCLUDED:
            existing.box = box
            existing.status = FbsHandoverOrder.STATUS_ACTIVE
            existing.exclusion_reason = ""
            existing.excluded_by = None
            existing.excluded_at = None
            existing.verified_label = None
            existing.verified_by = None
            existing.verified_at = None
            existing.added_by = actor
            existing.save(
                update_fields=[
                    "box",
                    "status",
                    "exclusion_reason",
                    "excluded_by",
                    "excluded_at",
                    "verified_label",
                    "verified_by",
                    "verified_at",
                    "added_by",
                ]
            )
            return existing
        if existing.status == FbsHandoverOrder.STATUS_RETURN_PENDING:
            raise FbsHandoverError("WB еще подтверждает перенос заказа в новую поставку.")
        if existing.box_id == box.id:
            return existing
        raise FbsHandoverError("Заказ уже находится в другом коробе поставки.")
    return FbsHandoverOrder.objects.create(box=box, order=order, added_by=actor)


@transaction.atomic
def verify_handover_order_label(
    *,
    batch_id: int,
    order_label_scan: str,
    verified_by,
) -> FbsHandoverOrder:
    _require_writes()
    actor = _authenticated_user(verified_by)
    if actor is None:
        raise FbsHandoverError("Не указан контролер, проверяющий состав отгрузки.")
    scan_value = str(order_label_scan or "").strip()
    if not scan_value:
        raise FbsHandoverError("Отсканируйте WB-этикетку заказа.")
    batch = (
        FbsHandoverBatch.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=batch_id)
    )
    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        raise FbsHandoverError("Контрольный скан в этом окне доступен только для WB.")
    if batch.status not in {
        FbsHandoverBatch.STATUS_OPEN,
        FbsHandoverBatch.STATUS_READY,
    }:
        raise FbsHandoverError("Отгрузка уже передана и недоступна для проверки.")
    label = (
        FbsOrderLabel.objects.select_for_update(of=("self",))
        .select_related("order")
        .filter(barcode=scan_value, marketplace=FbsIntegrationProfile.MARKETPLACE_WB)
        .order_by("-id")
        .first()
    )
    if label is None:
        raise FbsHandoverError("WB-этикетка заказа не найдена.")
    if label.status != FbsOrderLabel.STATUS_APPLIED:
        raise FbsHandoverError("WB-этикетка еще не подтверждена на рабочем столе.")
    assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update(of=("self",))
        .filter(batch=batch, order_id=label.order_id)
        .first()
    )
    if assignment is None:
        other_assignment = (
            FbsHandoverOrderAssignment.objects.select_related("batch")
            .filter(order_id=label.order_id)
            .first()
        )
        if other_assignment is not None:
            other_supply = (
                other_assignment.batch.external_supply_id
                or f"Fullbox #{other_assignment.batch_id}"
            )
            raise FbsHandoverError(
                f"Не ОК: заказ относится к другой поставке {other_supply}."
            )
        raise FbsHandoverError("Заказ не назначен в эту поставку WB.")
    if assignment.status != FbsHandoverOrderAssignment.STATUS_CONFIRMED:
        if assignment.error:
            raise FbsHandoverError(assignment.error)
        raise FbsHandoverError("Заказ еще не подтвержден в этой поставке WB.")
    link = (
        FbsHandoverOrder.objects.select_for_update(of=("self",))
        .select_related("box", "order")
        .filter(
            box__batch=batch,
            order_id=label.order_id,
            status=FbsHandoverOrder.STATUS_ACTIVE,
        )
        .first()
    )
    if link is None:
        other_link = (
            FbsHandoverOrder.objects.select_related("box__batch")
            .filter(order_id=label.order_id)
            .first()
        )
        if other_link is not None:
            other_supply = (
                other_link.box.batch.external_supply_id
                or f"Fullbox #{other_link.box.batch_id}"
            )
            raise FbsHandoverError(
                f"Не ОК: заказ по этой этикетке находится в другой поставке {other_supply}."
            )
        raise FbsHandoverError("Заказ по этой этикетке не добавлен в короб данной отгрузки.")
    if link.verified_at is not None:
        if link.verified_label_id == label.id:
            return link
        raise FbsHandoverError("Заказ уже проверен по другой WB-этикетке.")
    now = timezone.now()
    link.verified_label = label
    link.verified_by = actor
    link.verified_at = now
    link.save(update_fields=["verified_label", "verified_by", "verified_at"])
    return link


@transaction.atomic
def close_handover_box(*, box_id: int) -> FbsHandoverBox:
    _require_writes()
    box = FbsHandoverBox.objects.select_for_update().get(pk=box_id)
    workstation = (
        FbsWorkstation.objects.select_for_update()
        .filter(active_handover_box_id=box.id)
        .first()
    )
    if box.status == FbsHandoverBox.STATUS_CLOSED:
        return box
    if (
        box.status == FbsHandoverBox.STATUS_SCANNED
        and box.batch.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        and not wb_handover_uses_marketplace_boxes(box.batch)
    ):
        return box
    if box.status != FbsHandoverBox.STATUS_OPEN:
        raise FbsHandoverError("Короб нельзя закрыть в текущем статусе.")
    orders = list(
        box.orders.select_related("order").filter(
            status=FbsHandoverOrder.STATUS_ACTIVE
        )
    )
    if not orders:
        raise FbsHandoverError("Нельзя закрыть пустой короб.")
    _assert_wb_box_orders_verified(box)
    for link in orders:
        if link.order.internal_status != FbsOrder.STATUS_READY_FOR_HANDOVER:
            raise FbsHandoverError("В коробе есть заказ, не готовый к передаче.")
        if not _required_metadata_confirmed(
            link.order,
            handover_batch=box.batch,
        ):
            raise FbsHandoverError("Не все обязательные данные подтверждены маркетплейсом.")
    warehouse_wb = (
        box.batch.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        and not wb_handover_uses_marketplace_boxes(box.batch)
    )
    box.status = (
        FbsHandoverBox.STATUS_SCANNED
        if warehouse_wb
        else FbsHandoverBox.STATUS_CLOSED
    )
    box.save(update_fields=["status", "updated_at"])
    if workstation is not None:
        workstation.active_handover_box = None
        workstation.save(update_fields=["active_handover_box", "updated_at"])
    active_boxes = box.batch.boxes.filter(
        orders__status=FbsHandoverOrder.STATUS_ACTIVE
    ).distinct()
    if warehouse_wb and active_boxes.exists() and not active_boxes.exclude(
        status=FbsHandoverBox.STATUS_SCANNED
    ).exists():
        controller_flow_open = FbsControllerCheckTote.objects.filter(
            handover_batch=box.batch
        ).exclude(status=FbsControllerCheckTote.STATUS_CLOSED).exists()
        if not controller_flow_open:
            box.batch.status = FbsHandoverBatch.STATUS_READY
            box.batch.save(update_fields=["status", "updated_at"])
    return box


@transaction.atomic
def scan_handover_box(*, batch_id: int, box_qr_scan: str, scanned_by) -> FbsHandoverBox:
    _require_writes()
    actor = _authenticated_user(scanned_by)
    if actor is None:
        raise FbsHandoverError("Не указан кладовщик, сканирующий короб.")
    batch = FbsHandoverBatch.objects.select_for_update().get(pk=batch_id)
    if batch.status not in {FbsHandoverBatch.STATUS_OPEN, FbsHandoverBatch.STATUS_READY}:
        raise FbsHandoverError("Поставка недоступна для сканирования.")
    box = FbsHandoverBox.objects.select_for_update().filter(
        batch=batch,
        qr_code=str(box_qr_scan or "").strip(),
    ).first()
    if box is None:
        raise FbsHandoverError("QR короба не относится к этой поставке.")
    _assert_wb_box_orders_verified(box)
    if box.status == FbsHandoverBox.STATUS_SCANNED:
        return box
    if box.status != FbsHandoverBox.STATUS_CLOSED:
        raise FbsHandoverError("Сначала закройте короб и проверьте все заказы.")
    now = timezone.now()
    box.status = FbsHandoverBox.STATUS_SCANNED
    box.scanned_by = actor
    box.scanned_at = now
    box.save(update_fields=["status", "scanned_by", "scanned_at", "updated_at"])
    all_boxes = batch.boxes.filter(
        orders__status=FbsHandoverOrder.STATUS_ACTIVE
    ).distinct()
    if all_boxes.exists() and not all_boxes.exclude(status=FbsHandoverBox.STATUS_SCANNED).exists():
        if FbsControllerCheckTote.objects.filter(handover_batch=batch).exists():
            assert_handover_composition_ready(batch)
        batch.status = FbsHandoverBatch.STATUS_READY
        batch.save(update_fields=["status", "updated_at"])
    return box


def _assert_verified_ozon_auto_dispatch_preconditions(
    batch: FbsHandoverBatch,
) -> None:
    """Keep the no-extra-scan Ozon path narrower than manual dispatch."""
    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_OZON:
        raise FbsHandoverError(
            "Автоматическая передача без скана зоны доступна только для Ozon."
        )
    if _has_handover_verification_override(batch):
        raise FbsHandoverError(
            "Автоматическая передача Ozon недоступна при служебном разрешении."
        )
    check_tote = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .filter(handover_batch=batch)
        .first()
    )
    if (
        check_tote is None
        or check_tote.status != FbsControllerCheckTote.STATUS_CLOSED
        or check_tote.closed_by_id is None
        or check_tote.closed_at is None
    ):
        raise FbsHandoverError(
            "Автоматическая передача Ozon возможна только после полной проверки контролером."
        )

    assignments = list(
        batch.order_assignments.exclude(
            status=FbsHandoverOrderAssignment.STATUS_CANCELED
        ).values_list("order_id", "status")
    )
    if not assignments or any(
        status != FbsHandoverOrderAssignment.STATUS_CONFIRMED
        for _, status in assignments
    ):
        raise FbsHandoverError(
            "Не все заказы Ozon подтверждены в текущей поставке."
        )
    assignment_order_ids = {order_id for order_id, _ in assignments}

    links = list(
        FbsHandoverOrder.objects.select_for_update(of=("self",))
        .select_related("verified_label")
        .filter(
            box__batch=batch,
            status=FbsHandoverOrder.STATUS_ACTIVE,
        )
    )
    if {link.order_id for link in links} != assignment_order_ids:
        raise FbsHandoverError(
            "Состав заказов Ozon не совпадает с подтвержденным составом поставки."
        )
    for link in links:
        if (
            link.verified_at is None
            or link.verified_by_id is None
            or link.verified_label_id is None
            or link.verified_label.order_id != link.order_id
            or link.verified_label.status != FbsOrderLabel.STATUS_APPLIED
        ):
            raise FbsHandoverError(
                "Не все официальные этикетки заказов Ozon подтверждены сканированием."
            )

    packed_order_ids = set(
        FbsControllerToteOrder.objects.filter(
            check_tote=check_tote,
            order_id__in=assignment_order_ids,
            status=FbsControllerToteOrder.STATUS_PACKED,
            composition_checked_by__isnull=False,
            composition_checked_at__isnull=False,
        ).values_list("order_id", flat=True)
    )
    if packed_order_ids != assignment_order_ids:
        raise FbsHandoverError(
            "Не все заказы Ozon завершили контроль состава."
        )
    active_boxes = batch.boxes.filter(
        orders__status=FbsHandoverOrder.STATUS_ACTIVE,
    ).distinct()
    if not active_boxes.exists() or active_boxes.exclude(
        status=FbsHandoverBox.STATUS_CLOSED
    ).exists():
        raise FbsHandoverError("Не все короба Ozon безопасно закрыты.")
    assert_handover_composition_ready(batch)


@transaction.atomic
def dispatch_handover_batch(
    *,
    batch_id: int,
    dispatched_by,
    dispatch_location_scan: str = "",
    supply_label_print_job_id: int | None = None,
    verified_ozon_auto: bool = False,
) -> FbsHandoverBatch:
    _require_writes()
    actor = _authenticated_user(dispatched_by)
    if actor is None:
        raise FbsHandoverError("Не указан сотрудник, передающий поставку водителю.")
    batch = (
        FbsHandoverBatch.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=batch_id)
    )
    dispatch_confirmed_by_print = supply_label_print_job_id is not None
    if verified_ozon_auto and dispatch_confirmed_by_print:
        raise FbsHandoverError(
            "Нельзя одновременно подтверждать автоматическую передачу Ozon и печать поставки."
        )
    if dispatch_confirmed_by_print:
        from processing_app.models import ProcessingPrintJob

        print_job = (
            ProcessingPrintJob.objects.select_for_update()
            .filter(pk=supply_label_print_job_id)
            .first()
        )
        expected_card_id = f"fbs:handover-supply:{batch.id}"
        if print_job is None:
            raise FbsHandoverError("Подтверждение печати ШК поставки не найдено.")
        if print_job.status != ProcessingPrintJob.STATUS_PRINTED:
            raise FbsHandoverError("Принтер еще не подтвердил печать ШК поставки.")
        if not (
            print_job.card_id == expected_card_id
            or print_job.card_id.startswith(f"{expected_card_id}:reprint:")
        ):
            raise FbsHandoverError("Задание печати относится к другой поставке.")
    if batch.status == FbsHandoverBatch.STATUS_DISPATCHED:
        return batch
    if batch.status != FbsHandoverBatch.STATUS_READY:
        raise FbsHandoverError("Не все короба поставки отсканированы.")
    if verified_ozon_auto:
        _assert_verified_ozon_auto_dispatch_preconditions(batch)
    if _has_handover_verification_override(batch):
        _assert_handover_verification_override_preconditions(batch)
    if FbsControllerCheckTote.objects.filter(handover_batch=batch).exists():
        assert_handover_composition_ready(batch)
    if batch.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        assert_wb_handover_orders_verified(batch)
        if batch.marketplace_state != FbsHandoverBatch.MARKETPLACE_COMPLETE:
            raise FbsHandoverError("WB еще не подтвердил передачу поставки в доставку.")
        if not batch.supply_qr_code or not batch.supply_label_file:
            raise FbsHandoverError("QR поставки WB еще не получен.")
    from sklad.models import WarehouseLocation
    from sklad.services.operational_locations import (
        resolve_operational_location_scan,
    )

    fbs_otg_locations_exist = WarehouseLocation.objects.filter(
        warehouse_code="MSK",
        zone_code="OTG",
        is_active=True,
        is_topology_visible=False,
        is_fbs_visible=True,
    ).exists()
    if (
        fbs_otg_locations_exist
        and not dispatch_confirmed_by_print
        and not verified_ozon_auto
    ):
        if not str(dispatch_location_scan or "").strip():
            raise FbsHandoverError("Отсканируйте QR места FBS в зоне OTG.")
        required_slots = batch.boxes.filter(
            orders__status=FbsHandoverOrder.STATUS_ACTIVE,
        ).distinct().count()
        try:
            batch.dispatch_location = resolve_operational_location_scan(
                dispatch_location_scan,
                expected_zone="OTG",
                require_fbs=True,
                required_slots=required_slots,
                lock=True,
            )
        except ValidationError as exc:
            raise FbsHandoverError("; ".join(exc.messages)) from exc
    _mark_handover_verification_override_used(batch, used_by=actor)
    now = timezone.now()
    batch.status = FbsHandoverBatch.STATUS_DISPATCHED
    batch.dispatched_by = actor
    batch.dispatched_at = now
    batch.save(
        update_fields=[
            "status",
            "dispatch_location",
            "dispatched_by",
            "dispatched_at",
            "updated_at",
        ]
    )
    batch.boxes.update(status=FbsHandoverBox.STATUS_DISPATCHED, updated_at=now)
    dispatched_orders = list(
        FbsOrder.objects.filter(
            handover_order__box__batch=batch,
            handover_order__status=FbsHandoverOrder.STATUS_ACTIVE,
        )
        .select_related("profile__agency")
        .distinct()
    )
    previous_statuses = {order.pk: order.internal_status for order in dispatched_orders}
    FbsOrder.objects.filter(pk__in=[order.pk for order in dispatched_orders]).update(
        internal_status=FbsOrder.STATUS_HANDED_OVER,
        updated_at=now,
    )
    log_order_bulk_transition(
        dispatched_orders,
        previous_internal_status=previous_statuses,
        internal_status=FbsOrder.STATUS_HANDED_OVER,
        user=actor,
        source=(
            "dispatch_handover_batch:verified_ozon_auto"
            if verified_ozon_auto
            else (
                "dispatch_handover_batch:supply_label_print"
                if dispatch_confirmed_by_print
                else "dispatch_handover_batch"
            )
        ),
        occurred_at=now,
    )
    for order in dispatched_orders:
        try:
            from fbs.signals import order_billing_ready

            responses = order_billing_ready.send_robust(
                sender=dispatch_handover_batch,
                order=order,
                user=actor,
            )
            for _, response in responses:
                if isinstance(response, Exception):
                    raise response
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Unable to sync FBS shipping facts for order %s", order.pk
            )
    return batch


@transaction.atomic
def request_wb_handover_delivery(*, batch_id: int, requested_by=None):
    _require_writes()
    from .marketplace import schedule_wb_handover_delivery

    actor = _authenticated_user(requested_by)
    batch = (
        FbsHandoverBatch.objects.select_for_update()
        .select_related("profile")
        .get(pk=batch_id)
    )
    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        raise FbsHandoverError("Автоматическое подтверждение коробов доступно только для WB.")
    if batch.status not in {
        FbsHandoverBatch.STATUS_OPEN,
        FbsHandoverBatch.STATUS_READY,
    }:
        raise FbsHandoverError("Отгрузка уже передана и недоступна для отправки в WB.")
    if batch.order_assignments.exclude(
        status=FbsHandoverOrderAssignment.STATUS_CANCELED
    ).exclude(
        status=FbsHandoverOrderAssignment.STATUS_CONFIRMED
    ).exists():
        raise FbsHandoverError("Не все заказы подтверждены в поставке WB.")
    if _has_handover_verification_override(batch):
        _assert_handover_verification_override_preconditions(batch)
    assert_handover_composition_ready(batch)
    assert_wb_handover_orders_verified(batch)

    active_box_ids = list(
        batch.boxes.filter(orders__status=FbsHandoverOrder.STATUS_ACTIVE)
        .values_list("id", flat=True)
        .distinct()
    )
    boxes = list(
        batch.boxes.select_for_update()
        .filter(id__in=active_box_ids)
        .order_by("id")
        .prefetch_related("orders__order")
    )
    if not boxes:
        raise FbsHandoverError("В отгрузке WB нет транспортного короба.")
    marketplace_boxes = wb_handover_uses_marketplace_boxes(batch)
    allowed_statuses = {
        FbsHandoverBox.STATUS_OPEN,
        FbsHandoverBox.STATUS_CLOSED,
        FbsHandoverBox.STATUS_SCANNED,
    }
    links_by_box_id = {
        box.id: list(box.orders.filter(status=FbsHandoverOrder.STATUS_ACTIVE))
        for box in boxes
    }
    for box in boxes:
        if not links_by_box_id[box.id]:
            raise FbsHandoverError(
                f"Пустой короб {box.qr_code} блокирует отправку в WB."
            )
    for box in boxes:
        links = links_by_box_id[box.id]
        if box.status not in allowed_statuses:
            raise FbsHandoverError(
                f"Короб {box.qr_code} недоступен для отправки в текущем статусе."
            )
        if marketplace_boxes and not box.label_file:
            raise FbsHandoverError(
                f"WB еще не передал готовый QR транспортного короба {box.qr_code}."
            )
        _assert_wb_box_orders_verified(box)
        for link in links:
            if link.order.internal_status != FbsOrder.STATUS_READY_FOR_HANDOVER:
                raise FbsHandoverError("В коробе есть заказ, не готовый к передаче.")
            if not _required_metadata_confirmed(
                link.order,
                handover_batch=batch,
            ):
                raise FbsHandoverError(
                    "Не все обязательные КИЗы и сроки годности подтверждены маркетплейсом."
                )

    now = timezone.now()
    for box in boxes:
        if box.status == FbsHandoverBox.STATUS_SCANNED:
            continue
        box.status = FbsHandoverBox.STATUS_SCANNED
        box.scanned_by = actor
        box.scanned_at = now
        box.save(
            update_fields=["status", "scanned_by", "scanned_at", "updated_at"]
        )
    FbsWorkstation.objects.filter(
        active_handover_box_id__in=[box.id for box in boxes]
    ).update(
        active_handover_box=None,
        updated_by=actor,
        updated_at=now,
    )
    if batch.status != FbsHandoverBatch.STATUS_READY:
        batch.status = FbsHandoverBatch.STATUS_READY
        batch.save(update_fields=["status", "updated_at"])

    _mark_handover_verification_override_used(batch, used_by=actor)
    return schedule_wb_handover_delivery(
        batch_id=batch_id,
        requested_by=actor,
    )


def handover_manifest(*, batch_id: int) -> tuple[FbsHandoverManifestRow, ...]:
    boxes = (
        FbsHandoverBox.objects.filter(
            batch_id=batch_id,
            orders__status=FbsHandoverOrder.STATUS_ACTIVE,
        )
        .distinct()
        .order_by("id")
    )
    return tuple(
        FbsHandoverManifestRow(
            box_qr=box.qr_code,
            external_box_id=box.external_box_id,
            order_count=box.orders.filter(
                status=FbsHandoverOrder.STATUS_ACTIVE
            ).count(),
            status=box.status,
        )
        for box in boxes
    )


@transaction.atomic
def refresh_handover_acceptance(*, batch_id: int) -> FbsHandoverBatch:
    """Refreshes red/green handover control from already synchronized marketplace statuses."""
    batch = (
        FbsHandoverBatch.objects.select_for_update()
        .select_related("profile")
        .get(pk=batch_id)
    )
    if batch.status not in {
        FbsHandoverBatch.STATUS_DISPATCHED,
        FbsHandoverBatch.STATUS_PROBLEM,
        FbsHandoverBatch.STATUS_ACCEPTED,
    }:
        return batch
    now = timezone.now()
    any_problem = False
    all_accepted = True
    marketplace_payload = (
        batch.marketplace_payload
        if isinstance(batch.marketplace_payload, dict)
        else {}
    )
    wb_supply_rejected = bool(
        marketplace_payload.get("rejectDt")
        or str(marketplace_payload.get("rejectReason") or "").strip()
    )
    wb_supply_accepted = bool(
        batch.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        and marketplace_payload.get("scanDt")
        and not wb_supply_rejected
    )
    boxes = list(batch.boxes.select_for_update().prefetch_related("orders__order"))
    for box in boxes:
        active_links = list(
            box.orders.filter(status=FbsHandoverOrder.STATUS_ACTIVE)
        )
        if any(
            _normalized(link.order.marketplace_status)
            in MARKETPLACE_PROBLEM_STATUSES
            for link in active_links
        ):
            box.status = FbsHandoverBox.STATUS_PROBLEM
            box.problem_reason = "Маркетплейс сообщил проблемный статус заказа."
            any_problem = True
            all_accepted = False
        elif active_links and (
            wb_supply_accepted
            or all(
                handover_order_has_accepted_marketplace_status(
                    link.order,
                    marketplace=batch.profile.marketplace,
                )
                for link in active_links
            )
        ):
            box.status = FbsHandoverBox.STATUS_ACCEPTED
            box.accepted_at = box.accepted_at or now
            box.problem_reason = ""
        else:
            box.status = FbsHandoverBox.STATUS_DISPATCHED
            all_accepted = False
        box.save(update_fields=["status", "accepted_at", "problem_reason", "updated_at"])
    if any_problem:
        batch.status = FbsHandoverBatch.STATUS_PROBLEM
    elif boxes and all_accepted:
        batch.status = FbsHandoverBatch.STATUS_ACCEPTED
        batch.accepted_at = batch.accepted_at or now
    else:
        batch.status = FbsHandoverBatch.STATUS_DISPATCHED
    batch.save(update_fields=["status", "accepted_at", "updated_at"])
    if batch.status == FbsHandoverBatch.STATUS_ACCEPTED:
        try:
            from fbs.signals import delivery_accepted

            responses = delivery_accepted.send_robust(
                sender=refresh_handover_acceptance,
                batch=batch,
                user=None,
            )
            for _, response in responses:
                if isinstance(response, Exception):
                    raise response
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Unable to sync FBS delivery fact for handover batch %s",
                batch.pk,
            )
    return batch


def refresh_handover_acceptance_for_order(
    *,
    order_id: int,
    include_accepted: bool = True,
) -> tuple[FbsHandoverBatch, ...]:
    """Refresh active handovers that contain a synchronized order."""
    batch_statuses = [
        FbsHandoverBatch.STATUS_DISPATCHED,
        FbsHandoverBatch.STATUS_PROBLEM,
    ]
    if include_accepted:
        batch_statuses.append(FbsHandoverBatch.STATUS_ACCEPTED)
    batch_ids = tuple(
        FbsHandoverOrder.objects.filter(
            order_id=order_id,
            status=FbsHandoverOrder.STATUS_ACTIVE,
            box__batch__status__in=tuple(batch_statuses),
        )
        .order_by("box__batch_id")
        .values_list("box__batch_id", flat=True)
        .distinct()
    )
    return tuple(
        refresh_handover_acceptance(batch_id=batch_id)
        for batch_id in batch_ids
    )
