from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import OperationalError, transaction
from django.db.models import Case, CharField, Exists, IntegerField, OuterRef, Q, Value, When
from django.db.models.functions import Cast
from django.utils import timezone

from fbs.exceptions import (
    FbsError,
    FbsFeatureDisabled,
    FbsHandoverError,
    FbsIntegrationError,
    FbsLabelError,
)
from fbs.flags import feature_enabled
from fbs.integrations.contracts import (
    MarketplaceCommandSpec,
    OZON_CREATE_OR_GET_EXEMPLARS,
    OZON_FETCH_POSTING_LABEL,
    OZON_READ_EXEMPLAR_STATUS,
    OZON_READ_POSTING_AFTER_SHIP,
    OZON_SET_EXEMPLARS,
    OZON_SHIP_POSTING,
    WB_FETCH_ORDER_STICKER,
    WB_CREATE_HANDOVER_SUPPLY,
    WB_READ_HANDOVER_SUPPLIES,
    WB_ADD_ORDER_TO_HANDOVER,
    WB_CANCEL_ORDER,
    WB_READ_HANDOVER_ORDER_IDS,
    WB_READ_HANDOVER_BOXES,
    WB_CREATE_HANDOVER_BOXES,
    WB_FETCH_HANDOVER_BOX_LABEL,
    WB_DELIVER_HANDOVER,
    WB_READ_HANDOVER_SUPPLY,
    WB_FETCH_HANDOVER_SUPPLY_LABEL,
    WB_READ_ORDER_METADATA,
    WB_SET_ORDER_EXPIRATION,
    WB_SET_ORDER_SGTINS,
)
from fbs.integrations.http import (
    MarketplaceHttpResponse,
    MarketplaceTransport,
    RequestsMarketplaceTransport,
)
from fbs.integrations.ozon import (
    OZON_AWAITING_DELIVER,
    OZON_AWAITING_PACKAGING,
    build_ozon_create_exemplars_spec,
    build_ozon_exemplar_status_spec,
    build_ozon_label_spec,
    build_ozon_readback_spec,
    build_ozon_set_exemplars_spec,
    build_ozon_ship_spec,
    parse_ozon_create_exemplars_response,
    parse_ozon_exemplar_status_response,
    parse_ozon_label_response,
    parse_ozon_readback_response,
    parse_ozon_set_exemplars_response,
    parse_ozon_ship_response,
)
from fbs.integrations.wb import (
    build_wb_add_order_to_handover_spec,
    build_wb_cancel_order_spec,
    build_wb_create_handover_boxes_spec,
    build_wb_create_handover_supply_spec,
    build_wb_deliver_handover_spec,
    build_wb_expiration_spec,
    build_wb_handover_box_label_spec,
    build_wb_handover_supply_label_spec,
    build_wb_metadata_readback_spec,
    build_wb_read_handover_boxes_spec,
    build_wb_read_handover_order_ids_spec,
    build_wb_read_handover_supplies_spec,
    build_wb_read_handover_supply_spec,
    build_wb_sgtin_spec,
    build_wb_sticker_spec,
    parse_wb_metadata_response,
    parse_wb_created_handover_box_ids,
    parse_wb_handover_box_ids,
    parse_wb_png_label,
    parse_wb_supply_id,
    parse_wb_supply_order_ids,
    parse_wb_sticker_response,
)
from fbs.models import (
    FbsControllerCheckTote,
    FbsControllerToteOrder,
    FbsIntegrationProfile,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsMarketplaceCommand,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderLabel,
    FbsOrderTraceability,
    FbsPickBatch,
    FbsPickRestockRequest,
    FbsPickTask,
    FbsWorkstation,
)

from .labels import (
    confirm_preloaded_ozon_order_label_after_marketplace_advance,
    is_preloaded_ozon_order_label,
    prefetch_ozon_order_label_barcode,
    prefetch_wb_order_label_request,
    register_marketplace_label,
)
from .handover import (
    MARKETPLACE_PROBLEM_STATUSES,
    OZON_ACCEPTED_STATUSES,
    assert_handover_composition_ready,
    wb_handover_compatibility_key,
    wb_handover_uses_marketplace_boxes,
)


ACTIVE_COMMAND_STATUSES = (
    FbsMarketplaceCommand.STATUS_PENDING,
    FbsMarketplaceCommand.STATUS_RETRY,
)
INTERACTIVE_LABEL_COMMAND_TYPES = (
    WB_FETCH_ORDER_STICKER,
    OZON_FETCH_POSTING_LABEL,
)
RETRYABLE_HTTP_STATUSES = {408, 425, 429}
OZON_SHIP_READBACK_ERROR_MARKERS = {
    "POSTING_ALREADY_CANCELLED",
    "POSTING_ALREADY_SHIPPED",
}
WB_METADATA_WRITE_COMMANDS = {WB_SET_ORDER_SGTINS, WB_SET_ORDER_EXPIRATION}
WB_METADATA_COMMAND_TYPES = WB_METADATA_WRITE_COMMANDS | {WB_READ_ORDER_METADATA}
MARKETPLACE_QUEUE_PRIORITY_GROUPS = (
    (frozenset(INTERACTIVE_LABEL_COMMAND_TYPES), 1),
    (frozenset(WB_METADATA_WRITE_COMMANDS), 2),
    (frozenset({WB_READ_ORDER_METADATA}), 3),
)
MARKETPLACE_QUEUE_BACKGROUND_PRIORITY = 4
OZON_METADATA_WRITE_COMMANDS = {OZON_SET_EXEMPLARS}
METADATA_COMMAND_TYPES = {
    WB_SET_ORDER_SGTINS,
    WB_SET_ORDER_EXPIRATION,
    WB_READ_ORDER_METADATA,
    OZON_CREATE_OR_GET_EXEMPLARS,
    OZON_SET_EXEMPLARS,
    OZON_READ_EXEMPLAR_STATUS,
}
METADATA_WRITE_COMMANDS = WB_METADATA_WRITE_COMMANDS | OZON_METADATA_WRITE_COMMANDS
WB_HANDOVER_WRITE_COMMANDS = {
    WB_CREATE_HANDOVER_SUPPLY,
    WB_ADD_ORDER_TO_HANDOVER,
    WB_CANCEL_ORDER,
    WB_CREATE_HANDOVER_BOXES,
    WB_DELIVER_HANDOVER,
}
WB_SUPPLY_LOOKUP_OPERATION = "locate_order_supply"
WB_SUPPLY_LOOKUP_CANDIDATE_OPERATION = "locate_order_supply_candidate"
WB_SUPPLY_LOOKUP_MAX_CANDIDATES = 200
WB_COMPOSITION_SYNC_OPERATION = "reconcile_handover_composition"
WB_COMPOSITION_SYNC_INTERVAL = timedelta(minutes=5)
MARKETPLACE_DEADLOCK_RETRY_ATTEMPTS = 3
MARKETPLACE_DEADLOCK_RETRY_DELAY_SECONDS = 0.05
WB_TERMINAL_ORDER_STATUSES = {
    "complete",
    "cancel",
    "cancel_by_client",
    "canceled_by_client",
    "decline",
    "deliver",
}

WB_METADATA_NAMES = {
    "sgtin": "КИЗ",
    "expiration": "срок годности",
}
WB_METADATA_DECISION_LABELS = {
    "sgtinintroduced": "код уже введен в оборот и отклонен площадкой",
    "sgtinnogs": "в коде отсутствуют обязательные разделители GS",
    "sgtininvalidformat": "неверный формат Data Matrix",
    "sgtinhasnonlatinsymbols": (
        "код содержит кириллицу; переключите раскладку сканера на EN"
    ),
}

logger = logging.getLogger(__name__)


def _database_error_sqlstate(error: BaseException) -> str:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        sqlstate = str(
            getattr(current, "sqlstate", "")
            or getattr(current, "pgcode", "")
            or ""
        )
        if sqlstate:
            return sqlstate
        current = current.__cause__ or current.__context__
    return ""


def _is_database_deadlock(error: BaseException) -> bool:
    return _database_error_sqlstate(error) == "40P01"


def _is_database_lock_contention(error: BaseException) -> bool:
    return _database_error_sqlstate(error) in {"40P01", "55P03"}


def _wb_order_is_terminal(order: FbsOrder | None) -> bool:
    if order is None:
        return False
    statuses = {
        str(order.marketplace_status or "").strip().casefold(),
        str(order.marketplace_substatus or "").strip().casefold(),
    }
    return bool(statuses & WB_TERMINAL_ORDER_STATUSES)


def _cancel_excluded_wb_add_command(
    command: FbsMarketplaceCommand,
) -> bool:
    """Stop an order that the controller already removed from verification."""
    if command.command_type != WB_ADD_ORDER_TO_HANDOVER or command.order_id is None:
        return False
    assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update()
        .filter(
            batch_id=command.handover_batch_id,
            order_id=command.order_id,
        )
        .first()
    )
    has_restock = FbsPickRestockRequest.objects.filter(
        order_id=command.order_id,
    ).exclude(
        status=FbsPickRestockRequest.STATUS_CANCELED,
    ).exists()
    order_is_excluded = command.order.internal_status in {
        FbsOrder.STATUS_CANCELLED,
        FbsOrder.STATUS_EXCEPTION,
    }
    assignment_is_canceled = bool(
        assignment is not None
        and assignment.status == FbsHandoverOrderAssignment.STATUS_CANCELED
    )
    if not (has_restock or order_is_excluded or assignment_is_canceled):
        return False

    reason = (
        "Добавление в поставку WB отменено: заказ исключен контролером "
        "во время проверки."
    )
    command.status = FbsMarketplaceCommand.STATUS_CANCELLED
    command.error = reason
    command.next_attempt_at = None
    command.save(
        update_fields=["status", "error", "next_attempt_at", "updated_at"]
    )
    if assignment is not None and assignment.status in {
        FbsHandoverOrderAssignment.STATUS_PENDING,
        FbsHandoverOrderAssignment.STATUS_ERROR,
    }:
        assignment.status = FbsHandoverOrderAssignment.STATUS_CANCELED
        assignment.error = reason
        assignment.confirmed_at = None
        assignment.save(
            update_fields=["status", "error", "confirmed_at", "updated_at"]
        )
    return True


@dataclass(frozen=True)
class MarketplaceQueueResult:
    inspected: int
    confirmed: int
    retry: int
    failed: int
    conflict: int


def _require_module() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")


def _require_outbox() -> None:
    _require_module()
    if not feature_enabled("outbox"):
        raise FbsFeatureDisabled("Исходящие команды FBS выключены.")


def _canonical_hash(payload: dict) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _idempotency_key(
    order: FbsOrder,
    spec: MarketplaceCommandSpec,
    *,
    idempotency_scope: str = "",
) -> str:
    key = f"fbs-label:{order.id}:{spec.command_type}:v1"
    if idempotency_scope:
        key = f"{key}:{idempotency_scope}"
    return key


def _enqueue_spec(
    order: FbsOrder,
    spec: MarketplaceCommandSpec,
    *,
    idempotency_scope: str = "",
    requested_by=None,
    request_source: str = FbsMarketplaceCommand.SOURCE_AUTOMATIC,
) -> FbsMarketplaceCommand:
    payload = spec.payload
    payload_hash = _canonical_hash(payload)
    key = _idempotency_key(order, spec, idempotency_scope=idempotency_scope)
    existing = (
        FbsMarketplaceCommand.objects.select_for_update(of=("self",))
        .filter(profile=order.profile, idempotency_key=key)
        .first()
    )
    if existing is not None:
        if (
            existing.command_type != spec.command_type
            or existing.endpoint != spec.endpoint
            or existing.http_method != spec.http_method
            or existing.payload_hash != payload_hash
        ):
            raise FbsIntegrationError("Идемпотентная команда FBS содержит другой контракт.")
        return existing
    return FbsMarketplaceCommand.objects.create(
        profile=order.profile,
        order=order,
        command_type=spec.command_type,
        http_method=spec.http_method,
        endpoint=spec.endpoint,
        endpoint_version=spec.endpoint_version,
        idempotency_key=key,
        payload=payload,
        payload_hash=payload_hash,
        requested_by=requested_by,
        request_source=request_source,
    )


def _enqueue_handover_spec(
    batch: FbsHandoverBatch,
    spec: MarketplaceCommandSpec,
    *,
    order: FbsOrder | None = None,
    box: FbsHandoverBox | None = None,
    idempotency_scope: str = "",
    requested_by=None,
    context: dict | None = None,
) -> FbsMarketplaceCommand:
    request_payload = spec.payload
    payload = {**request_payload, "context": context or {}}
    payload_hash = _canonical_hash(request_payload)
    key = f"fbs-handover:{batch.id}:{spec.command_type}:v1"
    if idempotency_scope:
        readable_key = f"{key}:{idempotency_scope}"
        key = (
            readable_key
            if len(readable_key) <= 128
            else f"{key}:{hashlib.sha256(readable_key.encode('utf-8')).hexdigest()[:24]}"
        )
    existing = (
        FbsMarketplaceCommand.objects.select_for_update(of=("self",))
        .filter(profile=batch.profile, idempotency_key=key)
        .first()
    )
    if existing is not None:
        if (
            existing.endpoint != spec.endpoint
            or existing.http_method != spec.http_method
            or existing.payload_hash != payload_hash
        ):
            raise FbsIntegrationError("Идемпотентная команда поставки содержит другой контракт.")
        return existing
    return FbsMarketplaceCommand.objects.create(
        profile=batch.profile,
        order=order,
        handover_batch=batch,
        handover_box=box,
        command_type=spec.command_type,
        http_method=spec.http_method,
        endpoint=spec.endpoint,
        endpoint_version=spec.endpoint_version,
        idempotency_key=key,
        payload=payload,
        payload_hash=payload_hash,
        requested_by=requested_by,
    )


@transaction.atomic
def schedule_wb_handover_supply(*, batch_id: int, requested_by=None):
    _require_outbox()
    batch = (
        FbsHandoverBatch.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=batch_id)
    )
    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        raise FbsIntegrationError("Автоматическая поставка этого этапа поддерживает WB.")
    if not batch.profile.is_active or not batch.profile.outbox_enabled:
        raise FbsFeatureDisabled("Исходящие команды профиля FBS выключены.")
    if batch.external_supply_id:
        return None
    if not batch.external_name:
        batch.external_name = (
            f"FULLBOX-{batch.profile_id}-{timezone.now():%Y%m%d-%H%M%S}-{batch.id}"
        )
    batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_CREATING
    batch.save(update_fields=["external_name", "marketplace_state", "updated_at"])
    return _enqueue_handover_spec(
        batch,
        build_wb_create_handover_supply_spec(batch.external_name),
        requested_by=requested_by,
    )


@transaction.atomic
def schedule_wb_handover_order(*, assignment_id: int, requested_by=None):
    _require_outbox()
    assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update(of=("self",))
        .select_related("batch__profile", "order")
        .get(pk=assignment_id)
    )
    batch = assignment.batch
    if (
        assignment.status != FbsHandoverOrderAssignment.STATUS_PENDING
        or assignment.order.internal_status != FbsOrder.STATUS_PICKED
    ):
        return None
    if not batch.profile.is_active or not batch.profile.outbox_enabled:
        raise FbsFeatureDisabled("Исходящие команды профиля FBS выключены.")
    if not batch.external_supply_id:
        schedule_wb_handover_supply(batch_id=batch.id, requested_by=requested_by)
        return None
    return _enqueue_handover_spec(
        batch,
        build_wb_add_order_to_handover_spec(
            supply_id=batch.external_supply_id,
            order_id=assignment.order.external_order_id,
        ),
        order=assignment.order,
        idempotency_scope=f"order-{assignment.order_id}",
        requested_by=requested_by,
    )


@transaction.atomic
def schedule_wb_handover_composition_sync(*, batch_id: int, requested_by=None):
    """Queue one factual WB supply readback per batch and safety interval."""
    _require_outbox()
    batch = (
        FbsHandoverBatch.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=batch_id)
    )
    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        raise FbsIntegrationError("Сверка состава доступна только для поставки WB.")
    if not batch.profile.is_active or not batch.profile.outbox_enabled:
        raise FbsFeatureDisabled("Исходящие команды профиля FBS выключены.")
    if batch.status not in {
        FbsHandoverBatch.STATUS_OPEN,
        FbsHandoverBatch.STATUS_READY,
    } or not batch.external_supply_id:
        return None
    sync_interval_seconds = max(
        int(WB_COMPOSITION_SYNC_INTERVAL.total_seconds()),
        60,
    )
    interval_bucket = int(timezone.now().timestamp()) // sync_interval_seconds
    return _enqueue_handover_spec(
        batch,
        build_wb_read_handover_order_ids_spec(batch.external_supply_id),
        idempotency_scope=f"composition-sync-{interval_bucket}",
        requested_by=requested_by,
        context={"readback_operation": WB_COMPOSITION_SYNC_OPERATION},
    )


@transaction.atomic
def schedule_wb_order_exclusion(
    *, request_id: int, requested_by=None, force_retry: bool = False
):
    """Cancel or verify a WB order, then read back actual supply membership."""
    _require_outbox()
    from fbs.models import FbsPickRestockRequest

    request = (
        FbsPickRestockRequest.objects.select_for_update(of=("self",))
        .select_related("order__profile", "handover_assignment__batch")
        .get(pk=request_id)
    )
    if request.order_id is None or request.handover_assignment_id is None:
        raise FbsIntegrationError("Задание исключения заказа повреждено.")
    if request.status != FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE:
        return None
    batch = request.handover_assignment.batch
    order = request.order
    if not batch.external_supply_id:
        raise FbsIntegrationError("WB еще не вернул ID поставки заказа.")
    if not order.profile.is_active or not order.profile.outbox_enabled:
        raise FbsFeatureDisabled("Исходящие команды профиля FBS выключены.")
    context = {
        "readback_operation": "exclude_order_supply_absence",
        "restock_request_id": request.id,
    }
    if request.marketplace_action == FbsPickRestockRequest.MARKETPLACE_ACTION_SELLER_CANCEL:
        spec = build_wb_cancel_order_spec(order.external_order_id)
        command = _enqueue_handover_spec(
            batch,
            spec,
            order=order,
            idempotency_scope=f"exclude-order-{request.id}",
            requested_by=requested_by,
            context=context,
        )
    elif request.marketplace_action == FbsPickRestockRequest.MARKETPLACE_ACTION_VERIFY_CANCEL:
        command = _enqueue_handover_spec(
            batch,
            build_wb_read_handover_order_ids_spec(batch.external_supply_id),
            order=order,
            idempotency_scope=f"exclude-order-read-{request.id}",
            requested_by=requested_by,
            context=context,
        )
    else:
        raise FbsIntegrationError("Для исключения заказа не задано действие WB.")
    if force_retry and command.status in {
        FbsMarketplaceCommand.STATUS_FAILED,
        FbsMarketplaceCommand.STATUS_CONFLICT,
    }:
        command.status = FbsMarketplaceCommand.STATUS_RETRY
        command.next_attempt_at = timezone.now()
        command.error = ""
        command.save(update_fields=["status", "next_attempt_at", "error", "updated_at"])
    return command


RESTOCK_RECOVERY_EVENT = "fbs_cancel_readback_recovery"


def recover_stalled_client_cancellations(*, limit: int = 20, profile_ids=None) -> int:
    """Retry only readback for physically isolated buyer cancellations, at most 3 times."""
    _require_outbox()
    from sklad.models import WarehouseEvent
    from .pick_restock import order_is_client_canceled_by_marketplace
    if not feature_enabled("warehouse_writes"):
        return 0

    cutoff = timezone.now() - timedelta(minutes=10)
    exhausted = WarehouseEvent.objects.filter(
        event_type=RESTOCK_RECOVERY_EVENT, stock_context_type="fbs_pick_restock",
        stock_context_id=Cast(OuterRef("pk"), output_field=CharField()), payload__attempt__gte=3,
    )
    candidates = FbsPickRestockRequest.objects.annotate(_recovery_exhausted=Exists(exhausted)).filter(
        _recovery_exhausted=False,
        status__in=(FbsPickRestockRequest.STATUS_FAILED,
                    FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE),
        reason_code=FbsPickRestockRequest.REASON_CLIENT_CANCELED,
        marketplace_action=FbsPickRestockRequest.MARKETPLACE_ACTION_VERIFY_CANCEL,
        source_tote__isnull=False, updated_at__lt=cutoff,
        order__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
        order__profile__is_active=True, order__profile__outbox_enabled=True,
        handover_assignment__status=FbsHandoverOrderAssignment.STATUS_CANCELED,
        handover_assignment__batch__status__in=(FbsHandoverBatch.STATUS_OPEN, FbsHandoverBatch.STATUS_READY),
    ).exclude(handover_assignment__batch__marketplace_state=FbsHandoverBatch.MARKETPLACE_COMPLETE).exclude(
        order__internal_status=FbsOrder.STATUS_HANDED_OVER,
    ).exclude(reason="")
    if profile_ids is not None:
        candidates = candidates.filter(order__profile_id__in=profile_ids)
    recovered = 0
    # Rotate by the last attempt; permanently blocked requests cannot monopolize the batch.
    ids = list(candidates.order_by("updated_at", "id").values_list("id", flat=True)[:max(1, int(limit))])
    for request_id in ids:
        try:
            with transaction.atomic():
                request = candidates.select_for_update(of=("self",), skip_locked=True).select_related(
                    "order__profile", "handover_assignment__batch"
                ).filter(pk=request_id).first()
                if request is None or not order_is_client_canceled_by_marketplace(request.order):
                    continue
                attempts = WarehouseEvent.objects.filter(
                    event_type=RESTOCK_RECOVERY_EVENT, stock_context_type="fbs_pick_restock",
                    stock_context_id=str(request.id),
                )
                attempt_count = attempts.count()
                if attempt_count >= 3 or attempts.filter(occurred_at__gte=cutoff).exists():
                    continue
                if FbsMarketplaceCommand.objects.filter(
                    payload__context__restock_request_id=request.id,
                    status__in=(FbsMarketplaceCommand.STATUS_PENDING,
                                FbsMarketplaceCommand.STATUS_SENT, FbsMarketplaceCommand.STATUS_RETRY),
                ).exists():
                    continue
                batch = request.handover_assignment.batch
                if not batch.external_supply_id:
                    continue
                # A fresh GET receipt is required; an old confirmed command is not reused.
                command = _enqueue_handover_spec(
                    batch, build_wb_read_handover_order_ids_spec(batch.external_supply_id),
                    order=request.order,
                    idempotency_scope=f"cancel-recovery-{request.id}-{attempt_count + 1}",
                    context={"readback_operation": "exclude_order_supply_absence",
                             "restock_request_id": request.id},
                )
                request.status = FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE
                request.save(update_fields=["status", "updated_at"])
                WarehouseEvent.objects.create(
                    agency_id=request.order.profile.agency_id,
                    event_type=RESTOCK_RECOVERY_EVENT, stock_context_type="fbs_pick_restock",
                    stock_context_id=str(request.id), qty=0, occurred_at=timezone.now(),
                    payload={"command_id": command.id, "attempt": attempt_count + 1},
                )
                recovered += 1
        except FbsError:
            logger.exception("Unable to schedule cancellation readback for restock %s", request_id)
    return recovered


@transaction.atomic
def schedule_wb_handover_boxes(*, batch_id: int, amount: int, requested_by=None):
    _require_outbox()
    batch = (
        FbsHandoverBatch.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=batch_id)
    )
    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        raise FbsIntegrationError("Автоматическое создание коробов доступно только для WB.")
    if not batch.profile.is_active or not batch.profile.outbox_enabled:
        raise FbsFeatureDisabled("Исходящие команды профиля FBS выключены.")
    if batch.status != FbsHandoverBatch.STATUS_OPEN or not batch.external_supply_id:
        raise FbsIntegrationError("Сначала создайте открытую поставку WB.")
    if not wb_handover_uses_marketplace_boxes(batch):
        raise FbsIntegrationError(
            "Короба WB-MP разрешены только для поставок в ПВЗ. "
            "Для склада или СЦ используется QR поставки WB-GI."
        )
    if batch.order_assignments.exclude(
        status=FbsHandoverOrderAssignment.STATUS_CANCELED
    ).exclude(
        status=FbsHandoverOrderAssignment.STATUS_CONFIRMED
    ).exists():
        raise FbsIntegrationError("Не все заказы подтверждены в поставке WB.")
    if FbsMarketplaceCommand.objects.filter(
        handover_batch=batch,
        command_type=WB_CREATE_HANDOVER_BOXES,
        status__in=(
            FbsMarketplaceCommand.STATUS_PENDING,
            FbsMarketplaceCommand.STATUS_SENT,
            FbsMarketplaceCommand.STATUS_RETRY,
            FbsMarketplaceCommand.STATUS_CONFLICT,
        ),
    ).exists():
        raise FbsIntegrationError("Предыдущее создание коробов еще не завершено.")
    known_ids = sorted(
        value for value in batch.boxes.values_list("external_box_id", flat=True) if value
    )
    spec = build_wb_create_handover_boxes_spec(
        supply_id=batch.external_supply_id,
        amount=int(amount),
    )
    return _enqueue_handover_spec(
        batch,
        spec,
        idempotency_scope=f"boxes-{batch.boxes.count()}-{int(amount)}",
        requested_by=requested_by,
        context={"known_ids": known_ids, "amount": int(amount)},
    )


@transaction.atomic
def _schedule_controller_wb_box_if_ready(*, batch_id: int) -> None:
    """Keep one box for a wave-owned shipment or a legacy workstation flow."""
    batch = (
        FbsHandoverBatch.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=batch_id)
    )
    if (
        batch.status != FbsHandoverBatch.STATUS_OPEN
        or not batch.external_supply_id
        or batch.order_assignments.exclude(
            status=FbsHandoverOrderAssignment.STATUS_CANCELED
        ).exclude(
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED
        ).exists()
        or batch.boxes.filter(status=FbsHandoverBox.STATUS_OPEN).exists()
    ):
        return
    compatibility_key = str(batch.compatibility_key or "")
    from .shipment_policy import shipment_pick_batch_id

    if ":workstation:" not in compatibility_key and shipment_pick_batch_id(batch) is None:
        confirmed_order_ids = list(
            batch.order_assignments.filter(
                status=FbsHandoverOrderAssignment.STATUS_CONFIRMED
            ).values_list("order_id", flat=True)
        )
        if confirmed_order_ids:
            logger.error(
                "FBS WB batch %s is ready for a controller box but compatibility_key "
                "lacks ':workstation:'; external_supply_id=%s; confirmed order_ids=%s; "
                "compatibility_key=%r; no box was created because controller routing "
                "is incomplete",
                batch.id,
                batch.external_supply_id,
                confirmed_order_ids,
                compatibility_key,
            )
        return
    if wb_handover_uses_marketplace_boxes(batch):
        if FbsMarketplaceCommand.objects.filter(
            handover_batch=batch,
            command_type=WB_CREATE_HANDOVER_BOXES,
            status__in=(
                FbsMarketplaceCommand.STATUS_PENDING,
                FbsMarketplaceCommand.STATUS_SENT,
                FbsMarketplaceCommand.STATUS_RETRY,
                FbsMarketplaceCommand.STATUS_CONFLICT,
            ),
        ).exists():
            return
        schedule_wb_handover_boxes(batch_id=batch.id, amount=1)
        return
    suffix = uuid4().hex[:12].upper()
    FbsHandoverBox.objects.create(
        batch=batch,
        qr_code=f"FBS-WB-GI-BOX-{batch.id}-{suffix}",
    )


@transaction.atomic
def schedule_wb_handover_delivery(
    *,
    batch_id: int,
    requested_by=None,
    controller_auto_dispatch_check_tote_id: int | None = None,
):
    _require_outbox()
    batch = (
        FbsHandoverBatch.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=batch_id)
    )
    if batch.status != FbsHandoverBatch.STATUS_READY:
        raise FbsIntegrationError("Перед отправкой в WB проверьте все закрытые короба.")
    try:
        assert_handover_composition_ready(batch)
    except FbsHandoverError as exc:
        raise FbsIntegrationError(
            f"Перед отправкой в WB выполните контрольный скан и устраните ошибки. {exc}"
        ) from exc
    if FbsHandoverOrder.objects.filter(
        box__batch=batch,
        status=FbsHandoverOrder.STATUS_ACTIVE,
        verified_at__isnull=True,
    ).exists():
        raise FbsIntegrationError(
            "Перед отправкой в WB выполните контрольный скан всех этикеток заказов."
        )
    active_boxes = batch.boxes.filter(
        orders__status=FbsHandoverOrder.STATUS_ACTIVE
    ).distinct()
    missing_marketplace_box_label = (
        wb_handover_uses_marketplace_boxes(batch)
        and active_boxes.filter(label_file="").exists()
    )
    if (
        not active_boxes.exists()
        or active_boxes.exclude(status=FbsHandoverBox.STATUS_SCANNED).exists()
        or missing_marketplace_box_label
    ):
        raise FbsIntegrationError("Не все короба поставки закрыты и проверены.")
    command = _enqueue_handover_spec(
        batch,
        build_wb_deliver_handover_spec(batch.external_supply_id),
        requested_by=requested_by,
        context=(
            {
                "controller_auto_dispatch_check_tote_id": int(
                    controller_auto_dispatch_check_tote_id
                )
            }
            if controller_auto_dispatch_check_tote_id
            else None
        ),
    )
    if controller_auto_dispatch_check_tote_id:
        payload = dict(command.payload or {})
        context = dict(
            payload.get("context")
            if isinstance(payload.get("context"), dict)
            else {}
        )
        if not context.get("controller_auto_dispatch_check_tote_id"):
            context["controller_auto_dispatch_check_tote_id"] = int(
                controller_auto_dispatch_check_tote_id
            )
            payload["context"] = context
            command.payload = payload
            command.save(update_fields=["payload", "updated_at"])
    if command.status == FbsMarketplaceCommand.STATUS_CONFIRMED:
        marketplace_state = FbsHandoverBatch.MARKETPLACE_COMPLETE
    elif command.status in {
        FbsMarketplaceCommand.STATUS_FAILED,
        FbsMarketplaceCommand.STATUS_CANCELLED,
    }:
        marketplace_state = FbsHandoverBatch.MARKETPLACE_ERROR
    else:
        marketplace_state = FbsHandoverBatch.MARKETPLACE_DELIVERY_PENDING
    if batch.marketplace_state != marketplace_state:
        batch.marketplace_state = marketplace_state
        batch.save(update_fields=["marketplace_state", "updated_at"])
    return command


def _command_scope(transfers) -> str:
    raw = ":".join(sorted(transfer.idempotency_key for transfer in transfers))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _wb_metadata_rejection_error(*, key: str, raw_decision: str, errors) -> str:
    decision = str(raw_decision or "").strip()
    normalized = decision.casefold()
    metadata_name = WB_METADATA_NAMES.get(key, "метаданные")
    decision_label = WB_METADATA_DECISION_LABELS.get(normalized)
    if decision_label:
        detail = f"{decision_label} ({decision})"
    elif decision:
        detail = f"площадка вернула решение «{decision}»"
    else:
        detail = "площадка не указала причину"
    error = f"WB отклонил {metadata_name}: {detail}."
    if errors:
        error = f"{error} Детали WB: {errors}"
    return error


def _wb_deliver_metadata_rejections(payload) -> list[dict]:
    """Return item-level marking rejections from a WB deliver response."""
    if not isinstance(payload, dict):
        return []
    if str(payload.get("code") or "").strip().casefold() != "metavalidationfail":
        return []
    data = payload.get("data")
    orders = data.get("orders") if isinstance(data, dict) else None
    if not isinstance(orders, list):
        return []
    rejections = []
    for order_row in orders:
        if not isinstance(order_row, dict):
            continue
        order_id = str(order_row.get("id") or "").strip()
        details = order_row.get("metaDetails")
        if not order_id or not isinstance(details, list):
            continue
        for detail in details:
            if not isinstance(detail, dict):
                continue
            key = str(detail.get("key") or "").strip().casefold()
            decision = str(detail.get("decision") or "").strip()
            if key != "sgtin" or not decision:
                continue
            errors = detail.get("errors")
            rejections.append(
                {
                    "order_id": order_id,
                    "key": key,
                    "value": str(detail.get("value") or ""),
                    "decision": decision,
                    "errors": errors if isinstance(errors, list) else [],
                }
            )
    return rejections


@transaction.atomic
def _mark_wb_deliver_metadata_rejections(
    command_id: int,
    response: MarketplaceHttpResponse,
) -> FbsMarketplaceCommand | None:
    """Expose WB deliver KIZ errors immediately instead of retrying the supply."""
    payload = response.json_payload if isinstance(response.json_payload, dict) else {}
    rejections = _wb_deliver_metadata_rejections(payload)
    if not rejections:
        return None
    command = (
        FbsMarketplaceCommand.objects.select_for_update(of=("self",))
        .select_related("handover_batch")
        .get(pk=command_id)
    )
    if command.command_type != WB_DELIVER_HANDOVER or not command.handover_batch_id:
        return None

    rejected_order_numbers = []
    rejected_transfer_ids = []
    for rejection in rejections:
        transfers = list(
            FbsMarketplaceMetadataTransfer.objects.select_for_update()
            .filter(
                order_item__order__external_order_id=rejection["order_id"],
                order_item__order__handover_assignment__batch_id=command.handover_batch_id,
                metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
            )
            .select_related("order_item__order")
            .order_by("id")
        )
        exact = [row for row in transfers if row.value == rejection["value"]]
        targets = exact or transfers
        if not targets:
            continue
        error = _wb_metadata_rejection_error(
            key=rejection["key"],
            raw_decision=rejection["decision"],
            errors=rejection["errors"],
        )
        _set_transfer_state(
            targets,
            status=FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
            external_status=f"wb_rejected:{rejection['decision']}",
            error=error,
        )
        rejected_order_numbers.append(rejection["order_id"])
        rejected_transfer_ids.extend(row.id for row in targets)

    if not rejected_transfer_ids:
        return None
    rejected_order_numbers = sorted(set(rejected_order_numbers))
    command.status = FbsMarketplaceCommand.STATUS_CONFLICT
    command.http_status = response.status_code
    command.response_payload = payload
    command.error = (
        "WB отклонил КИЗ в заказах: "
        + ", ".join(rejected_order_numbers)
        + ". Повторно отсканируйте указанные КИЗы."
    )[:2000]
    command.next_attempt_at = None
    command.save(
        update_fields=[
            "status",
            "http_status",
            "response_payload",
            "error",
            "next_attempt_at",
            "updated_at",
        ]
    )
    FbsHandoverBatch.objects.filter(pk=command.handover_batch_id).update(
        marketplace_state=FbsHandoverBatch.MARKETPLACE_ERROR,
        updated_at=timezone.now(),
    )
    return command


def is_final_wb_marking_rejection(
    transfer: FbsMarketplaceMetadataTransfer | None,
) -> bool:
    """Return whether WB explicitly and finally rejected this exact KIZ."""
    if (
        transfer is None
        or transfer.metadata_type
        != FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE
        or transfer.status != FbsMarketplaceMetadataTransfer.STATUS_CONFLICT
    ):
        return False
    external_status = str(transfer.external_status or "").strip().casefold()
    if external_status.startswith("wb_rejected:"):
        return True
    return str(transfer.last_error or "").strip().casefold().startswith(
        "wb отклонил киз:"
    )


def _metadata_transfers_for_command(command: FbsMarketplaceCommand):
    queryset = FbsMarketplaceMetadataTransfer.objects.filter(
        order_item__order_id=command.order_id
    )
    if command.command_type == WB_SET_ORDER_SGTINS:
        return queryset.filter(metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE)
    if command.command_type == WB_SET_ORDER_EXPIRATION:
        return queryset.filter(metadata_type=FbsMarketplaceMetadataTransfer.TYPE_EXPIRATION)
    if command.command_type in {
        OZON_CREATE_OR_GET_EXEMPLARS,
        OZON_SET_EXEMPLARS,
        OZON_READ_EXEMPLAR_STATUS,
    }:
        return queryset.filter(metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE)
    if command.command_type == WB_READ_ORDER_METADATA:
        match = re.search(r":(?:after-|write-attempt-)(\d+)(?:-\d+)?$", command.idempotency_key)
        if match:
            source_type = (
                FbsMarketplaceCommand.objects.filter(pk=int(match.group(1)))
                .values_list("command_type", flat=True)
                .first()
            )
            if source_type == WB_SET_ORDER_SGTINS:
                return queryset.filter(
                    metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE
                )
            if source_type == WB_SET_ORDER_EXPIRATION:
                return queryset.filter(
                    metadata_type=FbsMarketplaceMetadataTransfer.TYPE_EXPIRATION
                )
        return queryset
    return queryset.none()


def _resume_wb_delivery_after_rejected_kiz_confirmation(transfers) -> None:
    """Resume a stopped WB deliver command once every rejected KIZ is confirmed."""
    order_ids = {
        transfer.order_item.order_id
        for transfer in transfers
        if transfer.metadata_type
        == FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE
    }
    if not order_ids:
        return
    batch_ids = list(
        FbsHandoverOrderAssignment.objects.filter(
            order_id__in=order_ids,
            batch__status=FbsHandoverBatch.STATUS_READY,
        )
        .values_list("batch_id", flat=True)
        .distinct()
    )
    for command in FbsMarketplaceCommand.objects.select_for_update().filter(
        handover_batch_id__in=batch_ids,
        command_type=WB_DELIVER_HANDOVER,
        status=FbsMarketplaceCommand.STATUS_CONFLICT,
    ):
        rejected_order_numbers = {
            row["order_id"]
            for row in _wb_deliver_metadata_rejections(command.response_payload)
        }
        if not rejected_order_numbers:
            continue
        unresolved = FbsMarketplaceMetadataTransfer.objects.filter(
            order_item__order__external_order_id__in=rejected_order_numbers,
            order_item__order__handover_assignment__batch_id=command.handover_batch_id,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        ).exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED)
        if unresolved.exists():
            continue
        command.status = FbsMarketplaceCommand.STATUS_RETRY
        command.error = "Исправленные КИЗы подтверждены WB; передача поставки возобновлена."
        command.next_attempt_at = timezone.now()
        command.save(
            update_fields=["status", "error", "next_attempt_at", "updated_at"]
        )
        FbsHandoverBatch.objects.filter(pk=command.handover_batch_id).update(
            marketplace_state=FbsHandoverBatch.MARKETPLACE_DELIVERY_PENDING,
            updated_at=timezone.now(),
        )


def _set_transfer_state(
    transfers,
    *,
    status: str,
    external_status: str = "",
    error: str = "",
    increment_attempt: bool = False,
) -> None:
    transfers = list(transfers)
    now = timezone.now()
    for transfer in transfers:
        transfer.status = status
        transfer.external_status = str(external_status or "")[:128]
        transfer.last_error = str(error or "")[:2000]
        if increment_attempt:
            transfer.attempt_count = int(transfer.attempt_count or 0) + 1
            transfer.sent_at = transfer.sent_at or now
        if status == FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED:
            transfer.confirmed_at = now
        transfer.save(
            update_fields=[
                "status",
                "external_status",
                "last_error",
                "attempt_count",
                "sent_at",
                "confirmed_at",
                "updated_at",
            ]
        )
    if status == FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED:
        _resume_wb_delivery_after_rejected_kiz_confirmation(transfers)
        from .picking import refresh_pick_batch_verification
        from .totes import refresh_check_tote_status

        batch_ids = FbsPickTask.objects.filter(
            order__items__metadata_transfers__in=transfers
        ).values_list("batch_id", flat=True).distinct()
        for batch_id in batch_ids:
            refresh_pick_batch_verification(batch_id=batch_id)
        check_tote_ids = (
            FbsControllerToteOrder.objects.filter(
                order__items__metadata_transfers__in=transfers,
                check_tote__status__in=(
                    FbsControllerCheckTote.STATUS_OPEN,
                    FbsControllerCheckTote.STATUS_WAITING_KIZ,
                    FbsControllerCheckTote.STATUS_READY,
                    FbsControllerCheckTote.STATUS_COMPOSITION,
                ),
            )
            .exclude(status=FbsControllerToteOrder.STATUS_REMOVED)
            .values_list("check_tote_id", flat=True)
            .distinct()
        )
        for check_tote_id in check_tote_ids:
            refresh_check_tote_status(check_tote_id=check_tote_id)


def _mark_command_transfers_sent(command: FbsMarketplaceCommand) -> None:
    if command.command_type not in METADATA_WRITE_COMMANDS:
        return
    transfers = list(
        _metadata_transfers_for_command(command)
        .select_for_update()
        .exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED)
    )
    _set_transfer_state(
        transfers,
        status=FbsMarketplaceMetadataTransfer.STATUS_SENT,
        external_status="request_sent",
        increment_attempt=True,
    )


def _sync_transfer_failure(command: FbsMarketplaceCommand) -> None:
    if command.command_type not in METADATA_COMMAND_TYPES:
        return
    mapping = {
        FbsMarketplaceCommand.STATUS_RETRY: FbsMarketplaceMetadataTransfer.STATUS_RETRY,
        FbsMarketplaceCommand.STATUS_FAILED: FbsMarketplaceMetadataTransfer.STATUS_FAILED,
        FbsMarketplaceCommand.STATUS_CONFLICT: FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
    }
    transfer_status = mapping.get(command.status)
    if transfer_status is None:
        return
    transfers = list(
        _metadata_transfers_for_command(command)
        .select_for_update()
        .exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED)
        .exclude(status=FbsMarketplaceMetadataTransfer.STATUS_UNSUPPORTED)
    )
    _set_transfer_state(
        transfers,
        status=transfer_status,
        external_status=f"command_{command.status}",
        error=command.error,
    )


def _queue_failed_wb_assignment_restock(
    *,
    assignment: FbsHandoverOrderAssignment,
    reason: str,
) -> None:
    """Queue a physical return only for a conclusive WB ownership conflict."""
    normalized_reason = str(reason or "").strip()
    reason_key = normalized_reason.casefold()
    if not any(
        marker in reason_key
        for marker in (
            "другую поставку wb",
            "сборочное задание уже завершено",
            "сборочное задание уже отменено",
            "сборочное задание уже передано",
        )
    ):
        return
    task = (
        FbsPickTask.objects.select_related("batch")
        .filter(
            order_id=assignment.order_id,
            status=FbsPickTask.STATUS_PICKED,
            batch__status=FbsPickBatch.STATUS_VERIFICATION,
            batch__picking_completed_at__isnull=False,
        )
        .order_by("-batch_id", "-id")
        .first()
    )
    if task is None or assignment.assigned_by_id is None:
        logger.warning(
            "FBS WB assignment %s cannot be queued for restock: task or actor is missing",
            assignment.id,
        )
        return
    from .pick_restock import create_order_pick_restock_request

    try:
        create_order_pick_restock_request(
            batch_id=task.batch_id,
            handover_batch_id=assignment.batch_id,
            order_id=assignment.order_id,
            reason_code=FbsPickRestockRequest.REASON_MARKETPLACE,
            reason=normalized_reason,
            confirm_seller_cancel=False,
            created_by=assignment.assigned_by,
        )
    except FbsError:
        logger.exception(
            "FBS WB assignment %s failed to queue an automatic pick return",
            assignment.id,
        )


def _sync_handover_failure(command: FbsMarketplaceCommand) -> None:
    if command.status not in {
        FbsMarketplaceCommand.STATUS_FAILED,
        FbsMarketplaceCommand.STATUS_CONFLICT,
    }:
        return
    terminal = command.status == FbsMarketplaceCommand.STATUS_FAILED
    context = _handover_command_context(command)
    restock_request_id = context.get("restock_request_id")
    if terminal and restock_request_id and command.command_type in {
        WB_CANCEL_ORDER,
        WB_READ_HANDOVER_ORDER_IDS,
    }:
        from .pick_restock import fail_order_pick_restock_marketplace

        fail_order_pick_restock_marketplace(
            request_id=int(restock_request_id),
            error=command.error,
        )
    if command.command_type == WB_ADD_ORDER_TO_HANDOVER and command.order_id:
        assignment = FbsHandoverOrderAssignment.objects.select_for_update().filter(
            batch_id=command.handover_batch_id,
            order_id=command.order_id,
        ).first()
        if assignment is not None:
            assignment.status = (
                FbsHandoverOrderAssignment.STATUS_ERROR
                if terminal
                else FbsHandoverOrderAssignment.STATUS_PENDING
            )
            assignment.error = command.error
            assignment.save(update_fields=["status", "error", "updated_at"])
            if terminal:
                from .pick_restock import invalid_kiz_reroute_context

                if invalid_kiz_reroute_context(assignment.batch) is None:
                    _queue_failed_wb_assignment_restock(
                        assignment=assignment,
                        reason=command.error,
                    )
    lookup_read_failed = command.status in {
        FbsMarketplaceCommand.STATUS_FAILED,
        FbsMarketplaceCommand.STATUS_CONFLICT,
    }
    if (
        lookup_read_failed
        and command.command_type == WB_READ_HANDOVER_ORDER_IDS
        and context.get("readback_operation") == WB_SUPPLY_LOOKUP_CANDIDATE_OPERATION
    ):
        _finalize_wb_order_supply_lookup(command)
    if (
        lookup_read_failed
        and command.command_type == WB_READ_HANDOVER_SUPPLIES
        and context.get("readback_operation") == WB_SUPPLY_LOOKUP_OPERATION
    ):
        write_command_id = context.get("write_command_id")
        if write_command_id:
            _retry_uncertain_write(
                int(write_command_id),
                "Не удалось получить список поставок WB; назначена безопасная повторная сверка.",
            )
    if not terminal:
        return
    if command.command_type in {
        WB_CREATE_HANDOVER_SUPPLY,
        WB_CREATE_HANDOVER_BOXES,
        WB_DELIVER_HANDOVER,
    } and command.handover_batch_id:
        FbsHandoverBatch.objects.filter(pk=command.handover_batch_id).update(
            marketplace_state=FbsHandoverBatch.MARKETPLACE_ERROR,
            updated_at=timezone.now(),
        )
    if command.command_type == WB_FETCH_HANDOVER_BOX_LABEL and command.handover_box_id:
        FbsHandoverBox.objects.filter(pk=command.handover_box_id).update(
            status=FbsHandoverBox.STATUS_PROBLEM,
            problem_reason=command.error,
            updated_at=timezone.now(),
        )


def _mark_schedule_error(transfers, error: str) -> None:
    _set_transfer_state(
        transfers,
        status=FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
        external_status="contract_rejected",
        error=error,
    )


@transaction.atomic
def schedule_marketplace_metadata(*, order_id: int) -> tuple[FbsMarketplaceCommand, ...]:
    _require_module()
    if not feature_enabled("outbox") or not feature_enabled("marking_push"):
        return tuple()
    order = (
        FbsOrder.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=order_id)
    )
    profile = order.profile
    if (
        order.internal_status != FbsOrder.STATUS_PICKED
        or not profile.is_active
        or not profile.outbox_enabled
        or not profile.marking_push_enabled
    ):
        return tuple()
    transfers = list(
        FbsMarketplaceMetadataTransfer.objects.select_for_update()
        .filter(
            order_item__order=order,
            status=FbsMarketplaceMetadataTransfer.STATUS_PREPARED,
        )
        .select_related("order_item")
        .order_by("metadata_type", "id")
    )
    if not transfers:
        return tuple()

    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        expiration = [
            transfer
            for transfer in transfers
            if transfer.metadata_type == FbsMarketplaceMetadataTransfer.TYPE_EXPIRATION
        ]
        if expiration:
            _set_transfer_state(
                expiration,
                status=FbsMarketplaceMetadataTransfer.STATUS_UNSUPPORTED,
                external_status="ozon_api_no_expiration_contract",
                error="В актуальном Ozon FBS API нет подтвержденного поля срока годности.",
            )
        marking = [
            transfer
            for transfer in transfers
            if transfer.metadata_type == FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE
        ]
        if not marking:
            return tuple()
        command = _enqueue_spec(
            order,
            build_ozon_create_exemplars_spec(order),
            idempotency_scope=_command_scope(marking),
        )
        _set_transfer_state(
            marking,
            status=FbsMarketplaceMetadataTransfer.STATUS_QUEUED,
            external_status="waiting_exemplar_ids",
        )
        return (command,)

    if profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        _mark_schedule_error(transfers, "Маркетплейс профиля FBS не поддерживается.")
        return tuple()
    if str(order.marketplace_status or "").strip().lower() != "confirm":
        for transfer in transfers:
            transfer.external_status = "waiting_wb_confirm"
            transfer.last_error = "Ожидается внешний статус WB confirm."
            transfer.save(update_fields=["external_status", "last_error", "updated_at"])
        return tuple()

    commands = []
    grouped = defaultdict(list)
    for transfer in transfers:
        grouped[transfer.metadata_type].append(transfer)
    for metadata_type, group in grouped.items():
        try:
            if metadata_type == FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE:
                spec = build_wb_sgtin_spec(order, [transfer.value for transfer in group])
            elif metadata_type == FbsMarketplaceMetadataTransfer.TYPE_EXPIRATION:
                values = {transfer.value for transfer in group}
                if len(values) != 1:
                    raise FbsIntegrationError("Для одного задания WB получены разные сроки годности.")
                spec = build_wb_expiration_spec(order, values.pop())
            else:
                raise FbsIntegrationError("Тип метаданных WB не поддерживается.")
        except FbsIntegrationError as exc:
            _mark_schedule_error(group, str(exc))
            continue
        command = _enqueue_spec(
            order,
            spec,
            idempotency_scope=_command_scope(group),
        )
        _set_transfer_state(
            group,
            status=FbsMarketplaceMetadataTransfer.STATUS_QUEUED,
            external_status="outbox_queued",
        )
        commands.append(command)
    return tuple(commands)


def retry_wb_marking_code(
    *,
    batch_id: int,
    order_item_id: int,
    marking_scan: str,
    requested_by=None,
) -> FbsMarketplaceCommand:
    """Replace a rejected WB marking scan without waiting on a busy shipment."""
    try:
        return _retry_wb_marking_code_once(
            batch_id=batch_id,
            order_item_id=order_item_id,
            marking_scan=marking_scan,
            requested_by=requested_by,
        )
    except OperationalError as exc:
        if not _is_database_lock_contention(exc):
            raise
        raise FbsIntegrationError(
            "С этой FBS-отгрузкой уже выполняется другая операция. "
            "Изменения не внесены; повторите сканирование через несколько секунд."
        ) from exc


@transaction.atomic
def _retry_wb_marking_code_once(
    *,
    batch_id: int,
    order_item_id: int,
    marking_scan: str,
    requested_by=None,
) -> FbsMarketplaceCommand:
    _require_outbox()
    if not feature_enabled("marking_push"):
        raise FbsFeatureDisabled("Передача КИЗов FBS выключена.")

    batch = (
        FbsHandoverBatch.objects.select_for_update(
            nowait=True,
            of=("self",),
        )
        .select_related("profile__agency")
        .get(pk=batch_id)
    )
    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        raise FbsIntegrationError("Повторное сканирование КИЗа доступно только для WB.")
    if batch.status not in {
        FbsHandoverBatch.STATUS_OPEN,
        FbsHandoverBatch.STATUS_READY,
    }:
        raise FbsIntegrationError(
            "КИЗ можно исправить только в открытой или готовой FBS-отгрузке."
        )
    if (
        not batch.profile.is_active
        or not batch.profile.outbox_enabled
        or not batch.profile.marking_push_enabled
    ):
        raise FbsIntegrationError("Профиль WB не разрешает повторную передачу КИЗа.")

    transfer = (
        FbsMarketplaceMetadataTransfer.objects.select_for_update(
            nowait=True,
            of=("self", "order_item__order"),
        )
        .select_related("order_item__order__profile")
        .filter(
            order_item_id=order_item_id,
            order_item__order__handover_assignment__batch=batch,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        )
        .first()
    )
    if transfer is None or transfer.traceability_id is None:
        raise FbsIntegrationError("Для этой позиции нет проблемной передачи КИЗа.")
    trace = (
        FbsOrderTraceability.objects.select_for_update(
            nowait=True,
            of=("self",),
        )
        .select_related("allocation__balance")
        .get(pk=transfer.traceability_id)
    )
    if transfer.status not in {
        FbsMarketplaceMetadataTransfer.STATUS_RETRY,
        FbsMarketplaceMetadataTransfer.STATUS_FAILED,
        FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
    }:
        raise FbsIntegrationError("Повторный скан разрешен только после ошибки передачи КИЗа.")

    order = transfer.order_item.order
    marketplace_status = str(order.marketplace_status or "").strip().casefold()
    if marketplace_status != "confirm":
        raise FbsIntegrationError(
            "WB не разрешает менять КИЗ: текущий статус заказа "
            f"«{order.marketplace_status or 'не получен'}», требуется confirm."
        )
    if FbsMarketplaceCommand.objects.select_for_update(
        nowait=True,
        of=("self",),
    ).filter(
        order=order,
        command_type__in=(WB_SET_ORDER_SGTINS, WB_READ_ORDER_METADATA),
        status__in=(
            FbsMarketplaceCommand.STATUS_PENDING,
            FbsMarketplaceCommand.STATUS_SENT,
            FbsMarketplaceCommand.STATUS_RETRY,
        ),
    ).exists():
        raise FbsIntegrationError(
            "По заказу уже выполняется обмен КИЗа с WB. Дождитесь результата."
        )

    from .picking import _restore_wb_kiz_gs_separators, _validate_kiz_scan

    # WB returns 204 before asynchronous validation. Reject obviously broken
    # scanner input here so a controller does not wait through readback retries
    # for Cyrillic keyboard input or two concatenated Data Matrix codes.
    # Do not enforce a GTIN/catalog match: bundled products and legacy client
    # catalogues may legitimately use a different outer product barcode.
    scanned_marking = _restore_wb_kiz_gs_separators(
        marking_scan,
        reference_markings=(trace.allocation.balance.marking_code,),
    )
    if not scanned_marking:
        raise FbsIntegrationError("Отсканируйте КИЗ Честного знака повторно.")
    try:
        scanned_marking = _validate_kiz_scan(scanned_marking)
    except FbsError as exc:
        raise FbsIntegrationError(str(exc)) from exc
    if len(scanned_marking) > FbsOrderTraceability._meta.get_field(
        "marking_code"
    ).max_length:
        raise FbsIntegrationError("КИЗ длиннее допустимого значения Fullbox.")

    old_value = str(transfer.value or "")
    separator_only_repair = bool(
        old_value
        and "\x1d" not in old_value
        and scanned_marking.count("\x1d") == 2
        and scanned_marking.replace("\x1d", "") == old_value
    )
    balance_marking = str(trace.allocation.balance.marking_code or "").strip()
    trace.marking_code = scanned_marking
    trace.full_clean()
    trace.save(update_fields=["marking_code", "updated_at"])

    raw_key = (
        f"{transfer.order_item_id}:{trace.id}:{transfer.metadata_type}:"
        f"{scanned_marking}"
    )
    transfer.value = scanned_marking
    transfer.idempotency_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    transfer.status = FbsMarketplaceMetadataTransfer.STATUS_PREPARED
    transfer.external_status = "rescanned_by_controller"
    transfer.last_error = ""
    transfer.sent_at = None
    transfer.confirmed_at = None
    transfer.save(
        update_fields=[
            "value",
            "idempotency_key",
            "status",
            "external_status",
            "last_error",
            "sent_at",
            "confirmed_at",
            "updated_at",
        ]
    )

    spec = build_wb_sgtin_spec(order, [scanned_marking])
    command = _enqueue_spec(
        order,
        spec,
        idempotency_scope=(
            f"manual-rescan-{transfer.id}-{transfer.attempt_count + 1}-"
            f"{uuid4().hex[:12]}"
        ),
        requested_by=requested_by,
        request_source=FbsMarketplaceCommand.SOURCE_METADATA_CONTROL,
    )
    _set_transfer_state(
        [transfer],
        status=FbsMarketplaceMetadataTransfer.STATUS_QUEUED,
        external_status="outbox_queued_after_rescan",
    )

    from audit.models import OrderAuditEntry

    OrderAuditEntry.objects.create(
        order_id=str(order.id),
        order_type="fbs_order",
        action="update",
        user=requested_by if getattr(requested_by, "is_authenticated", False) else None,
        agency=batch.profile.agency,
        description=(
            "Фактический КИЗ FBS принят после повторного сканирования "
            "и поставлен в очередь WB."
        ),
        payload={
            "source": "fbs_handover_marking_rescan",
            "batch_id": batch.id,
            "external_supply_id": batch.external_supply_id,
            "external_order_id": order.external_order_id,
            "order_item_id": transfer.order_item_id,
            "transfer_id": transfer.id,
            "command_id": command.id,
            "old_value_sha256": hashlib.sha256(old_value.encode("utf-8")).hexdigest(),
            "new_value_sha256": hashlib.sha256(scanned_marking.encode("utf-8")).hexdigest(),
            "accepted_physical_fact": bool(
                balance_marking and balance_marking != scanned_marking
            ),
            "gs_separator_only_repair": separator_only_repair,
            "balance_marking_unchanged": True,
            "skip_chat_bridge": True,
        },
    )
    return command


@transaction.atomic
def request_marketplace_metadata_readback(
    *,
    order_id: int,
    requested_by=None,
) -> FbsMarketplaceCommand:
    """Queue a read-only marketplace check without replaying metadata writes."""
    _require_outbox()
    if not feature_enabled("marking_push"):
        raise FbsFeatureDisabled("Передача маркировки FBS выключена.")

    order = (
        FbsOrder.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=order_id)
    )
    profile = order.profile
    if not profile.is_active or not profile.outbox_enabled or not profile.marking_push_enabled:
        raise FbsIntegrationError("Профиль FBS не разрешает сверку метаданных.")

    unresolved_statuses = (
        FbsMarketplaceMetadataTransfer.STATUS_SENT,
        FbsMarketplaceMetadataTransfer.STATUS_RETRY,
        FbsMarketplaceMetadataTransfer.STATUS_FAILED,
        FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
    )
    transfers = FbsMarketplaceMetadataTransfer.objects.select_for_update().filter(
        order_item__order=order,
        status__in=unresolved_statuses,
    )
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        transfers = transfers.filter(
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE
        )
        command_type = OZON_READ_EXEMPLAR_STATUS
        spec = build_ozon_exemplar_status_spec(order)
    elif profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        command_type = WB_READ_ORDER_METADATA
        spec = build_wb_metadata_readback_spec(order)
    else:
        raise FbsIntegrationError("Маркетплейс профиля FBS не поддерживается.")

    if not transfers.exists():
        raise FbsIntegrationError("Нет переданных данных, которые можно безопасно сверить.")

    active = (
        FbsMarketplaceCommand.objects.select_for_update(of=("self",))
        .filter(
            order=order,
            command_type=command_type,
            status__in=(
                FbsMarketplaceCommand.STATUS_PENDING,
                FbsMarketplaceCommand.STATUS_RETRY,
                FbsMarketplaceCommand.STATUS_SENT,
            ),
        )
        .order_by("-id")
        .first()
    )
    if active is not None:
        return active

    latest_command_id = (
        FbsMarketplaceCommand.objects.filter(
            order=order,
            command_type__in=METADATA_COMMAND_TYPES,
        )
        .order_by("-id")
        .values_list("id", flat=True)
        .first()
        or 0
    )
    return _enqueue_spec(
        order,
        spec,
        idempotency_scope=f"manual-after-{latest_command_id}",
        requested_by=requested_by,
        request_source=FbsMarketplaceCommand.SOURCE_METADATA_CONTROL,
    )


def _ozon_product_id(transfer: FbsMarketplaceMetadataTransfer) -> int:
    raw_payload = (
        transfer.order_item.raw_payload
        if isinstance(transfer.order_item.raw_payload, dict)
        else {}
    )
    raw_product_id = raw_payload.get("product_id")
    identifier_name = "product_id"
    if raw_product_id in (None, ""):
        raw_product_id = raw_payload.get("sku")
        identifier_name = "sku"
    try:
        product_id = int(raw_product_id)
    except (TypeError, ValueError) as exc:
        raise FbsIntegrationError(
            f"Строка {transfer.order_item.external_line_id}: отсутствует числовой "
            f"product_id или sku Ozon."
        ) from exc
    if product_id <= 0:
        raise FbsIntegrationError(
            f"{identifier_name} Ozon должен быть положительным числом."
        )
    return product_id


def _ozon_mandatory_mark_not_required(product: dict) -> bool:
    return bool(
        product.get("is_mandatory_mark_needed") is False
        and product.get("is_mandatory_mark_possible") is False
    )


def _cancel_ozon_redundant_mark_transfers(transfers) -> None:
    for transfer in transfers:
        transfer.is_required = False
        transfer.status = FbsMarketplaceMetadataTransfer.STATUS_CANCELED
        transfer.external_status = "ozon_mandatory_mark_not_required"
        transfer.last_error = ""
        transfer.save(
            update_fields=[
                "is_required",
                "status",
                "external_status",
                "last_error",
                "updated_at",
            ]
        )


def _ozon_marks(exemplar: dict) -> list[dict]:
    raw_marks = exemplar.get("marks")
    if not isinstance(raw_marks, list):
        return []
    return [dict(mark) for mark in raw_marks if isinstance(mark, dict)]


def _prepare_ozon_set_body(order: FbsOrder, payload: dict) -> dict | None:
    transfers = list(
        FbsMarketplaceMetadataTransfer.objects.select_for_update()
        .filter(
            order_item__order=order,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        )
        .exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED)
        .select_related("order_item")
        .order_by("order_item_id", "id")
    )
    if not transfers:
        raise FbsIntegrationError("Для отправления Ozon нет подготовленных КИЗов.")
    transfers_by_product = defaultdict(list)
    for transfer in transfers:
        transfers_by_product[_ozon_product_id(transfer)].append(transfer)

    products = payload.get("products")
    if not isinstance(products, list):
        raise FbsIntegrationError("Ozon не вернул список экземпляров товаров.")
    prepared_products = []
    seen_product_ids = set()
    for raw_product in products:
        if not isinstance(raw_product, dict):
            raise FbsIntegrationError("Ozon вернул товар экземпляра в неверном формате.")
        try:
            product_id = int(raw_product.get("product_id"))
        except (TypeError, ValueError) as exc:
            raise FbsIntegrationError("Ozon вернул экземпляр без product_id.") from exc
        seen_product_ids.add(product_id)
        product_transfers = transfers_by_product.get(product_id, [])
        if _ozon_mandatory_mark_not_required(raw_product):
            _cancel_ozon_redundant_mark_transfers(product_transfers)
            continue
        raw_exemplars = raw_product.get("exemplars")
        if not isinstance(raw_exemplars, list):
            raise FbsIntegrationError("Ozon не вернул экземпляры товара.")
        prepared_exemplars = []
        for raw_exemplar in raw_exemplars:
            if not isinstance(raw_exemplar, dict):
                raise FbsIntegrationError("Ozon вернул экземпляр в неверном формате.")
            try:
                exemplar_id = int(raw_exemplar.get("exemplar_id"))
            except (TypeError, ValueError) as exc:
                raise FbsIntegrationError("Ozon не вернул exemplar_id.") from exc
            prepared_exemplars.append(
                {
                    "exemplar_id": exemplar_id,
                    "gtd": str(raw_exemplar.get("gtd") or ""),
                    "is_gtd_absent": bool(raw_exemplar.get("is_gtd_absent", True)),
                    "is_rnpt_absent": bool(raw_exemplar.get("is_rnpt_absent", True)),
                    "marks": _ozon_marks(raw_exemplar),
                    "rnpt": str(raw_exemplar.get("rnpt") or ""),
                }
            )

        assigned_ids = set()
        for transfer in product_transfers:
            target = next(
                (
                    exemplar
                    for exemplar in prepared_exemplars
                    if any(
                        str(mark.get("mark") or "") == transfer.value
                        for mark in exemplar["marks"]
                    )
                ),
                None,
            )
            if target is None:
                target = next(
                    (
                        exemplar
                        for exemplar in prepared_exemplars
                        if exemplar["exemplar_id"] not in assigned_ids
                        and not any(
                            str(mark.get("mark_type") or "").lower() == "mandatory_mark"
                            and str(mark.get("mark") or "") != transfer.value
                            for mark in exemplar["marks"]
                        )
                    ),
                    None,
                )
            if target is None:
                raise FbsIntegrationError(
                    f"Ozon не вернул свободный exemplar_id для КИЗа строки "
                    f"{transfer.order_item.external_line_id}."
                )
            if not any(str(mark.get("mark") or "") == transfer.value for mark in target["marks"]):
                target["marks"].append(
                    {"mark": transfer.value, "mark_type": "mandatory_mark"}
                )
            assigned_ids.add(target["exemplar_id"])
            transfer.status = FbsMarketplaceMetadataTransfer.STATUS_QUEUED
            transfer.external_status = f"exemplar_id:{target['exemplar_id']}"
            transfer.last_error = ""
            transfer.save(
                update_fields=["status", "external_status", "last_error", "updated_at"]
            )
        prepared_products.append(
            {"product_id": product_id, "exemplars": prepared_exemplars}
        )

    missing_products = set(transfers_by_product) - seen_product_ids
    if missing_products:
        raise FbsIntegrationError(
            "Ozon не вернул экземпляры для product_id: "
            + ", ".join(str(value) for value in sorted(missing_products))
        )
    if not prepared_products:
        return None
    return {
        "multi_box_qty": max(int(payload.get("multi_box_qty") or 0), 0),
        "posting_number": order.external_order_id,
        "products": prepared_products,
    }


def _flatten_external_values(value) -> set[str]:
    if isinstance(value, dict):
        result = set()
        for nested in value.values():
            result.update(_flatten_external_values(nested))
        return result
    if isinstance(value, (list, tuple, set)):
        result = set()
        for nested in value:
            result.update(_flatten_external_values(nested))
        return result
    if value in (None, ""):
        return set()
    return {str(value).strip()}


def _apply_wb_metadata_readback(
    command: FbsMarketplaceCommand,
    metadata: dict,
    response: MarketplaceHttpResponse,
) -> FbsMarketplaceCommand:
    details = metadata["meta_details"]
    pending = []
    conflicts = []
    transfers = list(
        _metadata_transfers_for_command(command)
        .select_for_update()
        .exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED)
        .exclude(status=FbsMarketplaceMetadataTransfer.STATUS_UNSUPPORTED)
    )
    for transfer in transfers:
        key = (
            "sgtin"
            if transfer.metadata_type == FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE
            else "expiration"
        )
        detail = details.get(key)
        if detail is None and key == "sgtin":
            _set_transfer_state(
                [transfer],
                status=FbsMarketplaceMetadataTransfer.STATUS_RETRY,
                external_status="wb_decision_pending",
                error="WB пока не вернул решение по КИЗу.",
            )
            pending.append(key)
            continue
        if detail is None:
            transfer_status = (
                FbsMarketplaceMetadataTransfer.STATUS_CONFLICT
                if transfer.is_required
                else FbsMarketplaceMetadataTransfer.STATUS_UNSUPPORTED
            )
            _set_transfer_state(
                [transfer],
                status=transfer_status,
                external_status="metadata_not_available",
                error=f"WB не вернул metaDetails.{key}.",
            )
            if transfer.is_required:
                conflicts.append(key)
            continue
        raw_decision = str(detail.get("decision") or "").strip()
        decision = raw_decision.casefold()
        errors = detail.get("errors") or []
        values = _flatten_external_values(detail.get("value"))
        value_matches = transfer.value in values
        if key == "expiration" and values:
            try:
                expected = datetime.fromisoformat(transfer.value).strftime("%d.%m.%Y")
                value_matches = value_matches or expected in values
            except ValueError:
                pass
        sgtin_introduced_matches = (
            key == "sgtin"
            and decision == "sgtinintroduced"
            and value_matches
            and not errors
        )
        optional_allowed = decision == "optional" and not errors
        if (
            decision == "filled"
            # WB has already accepted the metadata write. ``optional`` means that
            # this field is not mandatory for the order, so WB may omit the value
            # from readback; absence of that echo is not a rejection.
            or optional_allowed
            or sgtin_introduced_matches
        ):
            _set_transfer_state(
                [transfer],
                status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
                external_status=decision,
            )
        elif decision in {"pending", "processing"}:
            _set_transfer_state(
                [transfer],
                status=FbsMarketplaceMetadataTransfer.STATUS_RETRY,
                external_status=decision,
                error="WB еще проверяет метаданные.",
            )
            pending.append(key)
        elif not raw_decision and not errors:
            _set_transfer_state(
                [transfer],
                status=FbsMarketplaceMetadataTransfer.STATUS_RETRY,
                external_status="wb_decision_pending",
                error="WB пока не вернул окончательное решение по метаданным.",
            )
            pending.append(key)
        else:
            error = _wb_metadata_rejection_error(
                key=key,
                raw_decision=raw_decision,
                errors=errors,
            )
            _set_transfer_state(
                [transfer],
                status=FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
                external_status=f"wb_rejected:{decision or 'error'}",
                error=error,
            )
            conflicts.append(key)
    if pending:
        command.http_status = response.status_code
        command.response_payload = metadata
        command.attempt_count = max(command.attempt_count, 1)
        _set_wb_metadata_readback_retry(
            command,
            "WB еще проверяет метаданные: " + ", ".join(pending),
        )
        command.save(
            update_fields=[
                "status",
                "http_status",
                "response_payload",
                "attempt_count",
                "error",
                "next_attempt_at",
                "updated_at",
            ]
        )
        if command.status == FbsMarketplaceCommand.STATUS_FAILED:
            _sync_transfer_failure(command)
        return command
    if conflicts:
        command.status = FbsMarketplaceCommand.STATUS_CONFLICT
        command.http_status = response.status_code
        command.response_payload = metadata
        command.error = "WB не подтвердил метаданные: " + ", ".join(conflicts)
        command.next_attempt_at = None
        command.save(
            update_fields=[
                "status",
                "http_status",
                "response_payload",
                "error",
                "next_attempt_at",
                "updated_at",
            ]
        )
        return command
    _confirm_command(command, http_status=response.status_code, response_payload=metadata)
    return command


OZON_PENDING_STATUSES = {"", "created", "pending", "processing", "in_progress", "checking"}
OZON_SUCCESS_STATUSES = {"success", "succeeded", "completed", "done", "ready"}
OZON_MARK_SUCCESS_STATUSES = {"success", "succeeded", "valid", "validated", "accepted", "checked", "ok"}


def _apply_ozon_exemplar_status(
    command: FbsMarketplaceCommand,
    metadata: dict,
    response: MarketplaceHttpResponse,
) -> FbsMarketplaceCommand:
    global_status = str(metadata.get("status") or "").strip().lower()
    marks_by_value = {}
    for product in metadata.get("products") or []:
        if not isinstance(product, dict):
            continue
        for exemplar in product.get("exemplars") or []:
            if not isinstance(exemplar, dict):
                continue
            for mark in exemplar.get("marks") or []:
                if isinstance(mark, dict) and str(mark.get("mark") or ""):
                    marks_by_value[str(mark["mark"])] = mark
    transfers = list(
        _metadata_transfers_for_command(command)
        .select_for_update()
        .exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED)
    )
    pending = []
    conflicts = []
    for transfer in transfers:
        mark = marks_by_value.get(transfer.value)
        if mark is None:
            if global_status in OZON_PENDING_STATUSES:
                _set_transfer_state(
                    [transfer],
                    status=FbsMarketplaceMetadataTransfer.STATUS_RETRY,
                    external_status=global_status or "pending",
                    error="Ozon еще не вернул результат проверки КИЗа.",
                )
                pending.append(transfer.id)
            else:
                _set_transfer_state(
                    [transfer],
                    status=FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
                    external_status="mark_not_returned",
                    error="Ozon не вернул переданный КИЗ в результате проверки.",
                )
                conflicts.append(transfer.id)
            continue
        check_status = str(mark.get("check_status") or "").strip().lower()
        errors = mark.get("error_codes") or []
        if errors:
            _set_transfer_state(
                [transfer],
                status=FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
                external_status=check_status or "rejected",
                error=f"Ozon отклонил КИЗ: {errors}",
            )
            conflicts.append(transfer.id)
        elif check_status in OZON_MARK_SUCCESS_STATUSES and global_status in OZON_SUCCESS_STATUSES:
            _set_transfer_state(
                [transfer],
                status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
                external_status=check_status,
            )
        elif check_status in OZON_PENDING_STATUSES or global_status in OZON_PENDING_STATUSES:
            _set_transfer_state(
                [transfer],
                status=FbsMarketplaceMetadataTransfer.STATUS_RETRY,
                external_status=check_status or global_status or "pending",
                error="Ozon еще проверяет КИЗ.",
            )
            pending.append(transfer.id)
        else:
            _set_transfer_state(
                [transfer],
                status=FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
                external_status=check_status or global_status or "unknown",
                error="Ozon вернул неподтвержденный статус КИЗа.",
            )
            conflicts.append(transfer.id)
    if pending:
        command.http_status = response.status_code
        command.response_payload = metadata
        _set_retry_or_failed(command, "Ozon еще проверяет переданные КИЗы.")
        command.save(
            update_fields=[
                "status",
                "http_status",
                "response_payload",
                "error",
                "next_attempt_at",
                "updated_at",
            ]
        )
        if command.status == FbsMarketplaceCommand.STATUS_FAILED:
            _sync_transfer_failure(command)
        return command
    if conflicts:
        command.status = FbsMarketplaceCommand.STATUS_CONFLICT
        command.http_status = response.status_code
        command.response_payload = metadata
        command.error = "Ozon не подтвердил все переданные КИЗы."
        command.next_attempt_at = None
        command.save(
            update_fields=[
                "status",
                "http_status",
                "response_payload",
                "error",
                "next_attempt_at",
                "updated_at",
            ]
        )
        return command
    _confirm_command(command, http_status=response.status_code, response_payload=metadata)
    schedule_label_preparation(order_id=command.order_id)
    return command


def _set_label_wait_reason(label: FbsOrderLabel, reason: str) -> None:
    if label.error == reason:
        return
    label.error = reason
    label.save(update_fields=["error", "updated_at"])


def _ozon_ship_response_requires_readback(
    response: MarketplaceHttpResponse,
) -> bool:
    """Recognize Ozon's HTTP 400 states that must be reconciled with posting/get."""
    payload = response.json_payload
    if isinstance(payload, (dict, list)):
        payload_text = json.dumps(payload, ensure_ascii=False)
    else:
        payload_text = str(payload or "")
    normalized = payload_text.upper()
    return any(
        marker in normalized for marker in OZON_SHIP_READBACK_ERROR_MARKERS
    )


@transaction.atomic
def schedule_label_preparation(*, order_id: int) -> FbsMarketplaceCommand | None:
    _require_module()
    if not feature_enabled("outbox"):
        return None
    order = (
        FbsOrder.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=order_id)
    )
    profile = order.profile
    if order.internal_status != FbsOrder.STATUS_PICKED:
        return None
    label = (
        FbsOrderLabel.objects.select_for_update()
        .filter(order=order, status=FbsOrderLabel.STATUS_REQUESTED)
        .order_by("id")
        .first()
    )
    if label is None:
        return None
    label_payload = label.payload if isinstance(label.payload, dict) else {}
    if (
        is_preloaded_ozon_order_label(label)
        and not label_payload.get("_preconfirmed_order_barcode_at")
    ):
        _set_label_wait_reason(
            label,
            "Ожидается скан ШК товара, ЧЗ и QR заказа.",
        )
        return None
    if not profile.is_active:
        _set_label_wait_reason(
            label,
            "Профиль маркетплейса клиента отключен. Обратитесь к начальнику склада.",
        )
        return None
    if not profile.outbox_enabled:
        _set_label_wait_reason(
            label,
            "В профиле клиента отключена отправка команд маркетплейсу. "
            "Этикетка не может быть создана до включения FBS-команд.",
        )
        return None

    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        assignment = (
            FbsHandoverOrderAssignment.objects.select_for_update()
            .filter(order=order)
            .order_by("id")
            .first()
        )
        if assignment is None:
            _set_label_wait_reason(
                label,
                "Ожидается создание поставки WB для заказа.",
            )
            return None
        if assignment.status != FbsHandoverOrderAssignment.STATUS_CONFIRMED:
            _set_label_wait_reason(
                label,
                str(assignment.error or "Ожидается подтверждение заказа в поставке WB."),
            )
            return None

    marketplace_status = str(order.marketplace_status or "").strip().lower()
    if (
        profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
        and marketplace_status == OZON_AWAITING_PACKAGING
        and FbsMarketplaceMetadataTransfer.objects.filter(
            order_item__order=order,
            is_required=True,
        )
        .exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED)
        .exists()
    ):
        unsupported_required = FbsMarketplaceMetadataTransfer.objects.filter(
            order_item__order=order,
            is_required=True,
            status=FbsMarketplaceMetadataTransfer.STATUS_UNSUPPORTED,
        ).exists()
        _set_label_wait_reason(
            label,
            (
                "Обязательные метаданные нельзя передать: актуальный Ozon FBS API "
                "не поддерживает срок годности."
                if unsupported_required
                else "Ожидается подтверждение передачи обязательных КИЗов и сроков годности в Ozon."
            ),
        )
        return None
    try:
        if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
            spec = build_wb_sticker_spec(order)
        elif profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
            if marketplace_status in MARKETPLACE_PROBLEM_STATUSES:
                _set_label_wait_reason(
                    label,
                    "Ozon отменил заказ. Ожидается возврат отобранного товара.",
                )
                return None
            if marketplace_status == OZON_AWAITING_PACKAGING:
                spec = build_ozon_ship_spec(order)
            elif marketplace_status in OZON_ACCEPTED_STATUSES:
                # Ozon no longer serves a package-label PDF after a posting has
                # advanced into delivery. Re-read the live posting and safely
                # confirm only the QR that the controller already scanned.
                spec = build_ozon_readback_spec(order)
            else:
                spec = build_ozon_label_spec(order)
        else:
            raise FbsIntegrationError("Маркетплейс профиля FBS не поддерживается.")
    except FbsIntegrationError as exc:
        _set_label_wait_reason(label, str(exc))
        return None

    command = _enqueue_spec(order, spec)
    if label.error:
        label.error = ""
        label.save(update_fields=["error", "updated_at"])
    return command


def _retry_delay(attempt_count: int) -> timedelta:
    seconds = min(30 * (2 ** max(attempt_count - 1, 0)), 3600)
    return timedelta(seconds=seconds)


def _wb_metadata_readback_retry_delay(attempt_count: int) -> timedelta:
    """Poll a fresh WB metadata decision quickly, then back off safely."""
    fast_seconds = (2, 5, 10, 20, 30, 60)
    index = max(int(attempt_count or 1) - 1, 0)
    if index < len(fast_seconds):
        return timedelta(seconds=fast_seconds[index])
    extra_attempts = min(index - len(fast_seconds), 5)
    return timedelta(seconds=min(120 * (2**extra_attempts), 3600))


def _max_attempts() -> int:
    return max(int(getattr(settings, "FBS_MARKETPLACE_MAX_ATTEMPTS", 8)), 1)


def _is_wb_metadata_command(command: FbsMarketplaceCommand) -> bool:
    return bool(
        command.command_type in WB_METADATA_COMMAND_TYPES
        and command.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
    )


def _set_retry_or_failed(command: FbsMarketplaceCommand, error: str) -> None:
    command.error = str(error or "Временная ошибка маркетплейса.")[:2000]
    if (
        command.attempt_count >= _max_attempts()
        and not _is_wb_metadata_command(command)
    ):
        command.status = FbsMarketplaceCommand.STATUS_FAILED
        command.next_attempt_at = None
        return
    command.status = FbsMarketplaceCommand.STATUS_RETRY
    command.next_attempt_at = timezone.now() + _retry_delay(command.attempt_count)


def _set_wb_metadata_readback_retry(
    command: FbsMarketplaceCommand,
    error: str,
) -> None:
    _set_retry_or_failed(command, error)
    if command.status == FbsMarketplaceCommand.STATUS_RETRY:
        command.next_attempt_at = (
            timezone.now()
            + _wb_metadata_readback_retry_delay(command.attempt_count)
        )


@transaction.atomic
def _mark_retry(
    command_id: int,
    error: str,
    *,
    expected_status: str | None = None,
) -> FbsMarketplaceCommand:
    command = FbsMarketplaceCommand.objects.select_for_update().get(pk=command_id)
    if expected_status is not None and command.status != expected_status:
        return command
    _set_retry_or_failed(command, error)
    command.save(update_fields=["status", "error", "next_attempt_at", "updated_at"])
    _sync_transfer_failure(command)
    _sync_handover_failure(command)
    return command


@transaction.atomic
def _defer_ozon_ship_to_readback(
    command_id: int,
    error: str,
    response: MarketplaceHttpResponse | None = None,
    *,
    expected_status: str | None = None,
) -> FbsMarketplaceCommand:
    command = (
        FbsMarketplaceCommand.objects.select_for_update(of=("self",))
        .select_related("order__profile", "profile")
        .get(pk=command_id)
    )
    if expected_status is not None and command.status != expected_status:
        return command
    if command.order is None:
        return _mark_retry(command.id, error)
    command.status = FbsMarketplaceCommand.STATUS_CONFLICT
    command.error = str(error or "Результат отправки Ozon не определен.")[:2000]
    command.next_attempt_at = None
    update_fields = ["status", "error", "next_attempt_at", "updated_at"]
    if response is not None:
        command.http_status = response.status_code
        command.response_payload = (
            response.json_payload
            if isinstance(response.json_payload, (dict, list))
            else {}
        )
        update_fields.extend(["http_status", "response_payload"])
    command.save(update_fields=update_fields)
    _enqueue_spec(
        command.order,
        build_ozon_readback_spec(command.order),
        idempotency_scope=f"ship-attempt-{command.attempt_count}",
    )
    return command


@transaction.atomic
def _defer_metadata_write_to_readback(
    command_id: int,
    error: str,
    response: MarketplaceHttpResponse | None = None,
    *,
    expected_status: str | None = None,
) -> FbsMarketplaceCommand:
    command = (
        FbsMarketplaceCommand.objects.select_for_update(of=("self",))
        .select_related("order__profile", "profile")
        .get(pk=command_id)
    )
    if expected_status is not None and command.status != expected_status:
        return command
    if command.order is None:
        return _mark_retry(command.id, error)
    command.status = FbsMarketplaceCommand.STATUS_CONFLICT
    command.error = str(error or "Результат передачи метаданных не определен.")[:2000]
    command.next_attempt_at = None
    update_fields = ["status", "error", "next_attempt_at", "updated_at"]
    if response is not None:
        command.http_status = response.status_code
        command.response_payload = (
            response.json_payload
            if isinstance(response.json_payload, (dict, list))
            else {}
        )
        update_fields.extend(["http_status", "response_payload"])
    command.save(update_fields=update_fields)
    transfers = list(
        _metadata_transfers_for_command(command)
        .select_for_update()
        .exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED)
    )
    _set_transfer_state(
        transfers,
        status=FbsMarketplaceMetadataTransfer.STATUS_SENT,
        external_status="result_unknown_readback_queued",
        error=command.error,
    )
    if command.command_type in WB_METADATA_WRITE_COMMANDS:
        readback_spec = build_wb_metadata_readback_spec(command.order)
    elif command.command_type == OZON_SET_EXEMPLARS:
        readback_spec = build_ozon_exemplar_status_spec(command.order)
    else:
        return command
    _enqueue_spec(
        command.order,
        readback_spec,
        idempotency_scope=f"write-attempt-{command.id}-{command.attempt_count}",
    )
    return command


@transaction.atomic
def _defer_wb_handover_write_to_readback(
    command_id: int,
    error: str,
    response: MarketplaceHttpResponse | None = None,
    *,
    expected_status: str | None = None,
) -> FbsMarketplaceCommand:
    command = (
        FbsMarketplaceCommand.objects.select_for_update(of=("self",))
        .select_related("handover_batch", "handover_box", "order")
        .get(pk=command_id)
    )
    if expected_status is not None and command.status != expected_status:
        return command
    batch = command.handover_batch
    if batch is None:
        return _mark_retry(command.id, error)
    command.status = FbsMarketplaceCommand.STATUS_CONFLICT
    command.error = str(error or "Результат команды поставки WB не определен.")[:2000]
    command.next_attempt_at = None
    update_fields = ["status", "error", "next_attempt_at", "updated_at"]
    if response is not None:
        command.http_status = response.status_code
        command.response_payload = (
            response.json_payload
            if isinstance(response.json_payload, (dict, list))
            else {}
        )
        update_fields.extend(["http_status", "response_payload"])
    command.save(update_fields=update_fields)
    command_context = _handover_command_context(command)
    readback_context = {"write_command_id": command.id}
    if command_context.get("controller_auto_dispatch_check_tote_id"):
        readback_context["controller_auto_dispatch_check_tote_id"] = int(
            command_context["controller_auto_dispatch_check_tote_id"]
        )
    if command.command_type == WB_CREATE_HANDOVER_SUPPLY:
        spec = build_wb_read_handover_supplies_spec()
    elif command.command_type == WB_ADD_ORDER_TO_HANDOVER:
        spec = build_wb_read_handover_supply_spec(batch.external_supply_id)
        readback_context["readback_operation"] = "add_order_supply_presence"
    elif command.command_type == WB_CANCEL_ORDER:
        spec = build_wb_read_handover_order_ids_spec(batch.external_supply_id)
        readback_context.update(
            {
                "readback_operation": "exclude_order_supply_absence",
                "restock_request_id": command_context.get("restock_request_id"),
            }
        )
    elif command.command_type == WB_CREATE_HANDOVER_BOXES:
        spec = build_wb_read_handover_boxes_spec(batch.external_supply_id)
    elif command.command_type == WB_DELIVER_HANDOVER:
        spec = build_wb_read_handover_supply_spec(batch.external_supply_id)
    else:
        return command
    _enqueue_handover_spec(
        batch,
        spec,
        order=command.order,
        box=command.handover_box,
        idempotency_scope=f"readback-{command.id}-{command.attempt_count}",
        context=readback_context,
    )
    return command


def _handover_command_context(command: FbsMarketplaceCommand) -> dict:
    payload = command.payload if isinstance(command.payload, dict) else {}
    return payload.get("context") if isinstance(payload.get("context"), dict) else {}


def _controller_auto_dispatch_check_tote_id(
    command: FbsMarketplaceCommand,
) -> int | None:
    raw_value = _handover_command_context(command).get(
        "controller_auto_dispatch_check_tote_id"
    )
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@transaction.atomic
def auto_dispatch_verified_wb_handover(
    *,
    batch_id: int,
) -> FbsHandoverBatch | None:
    """Dispatch only WB batches explicitly marked by a new controller close."""
    marker_command = None
    check_tote_id = None
    commands = (
        FbsMarketplaceCommand.objects.select_for_update(of=("self",))
        .filter(
            handover_batch_id=batch_id,
            command_type=WB_DELIVER_HANDOVER,
        )
        .order_by("-id")
    )
    for candidate in commands:
        candidate_check_tote_id = _controller_auto_dispatch_check_tote_id(
            candidate
        )
        if candidate_check_tote_id is not None:
            marker_command = candidate
            check_tote_id = candidate_check_tote_id
            break
    if marker_command is None or check_tote_id is None:
        return None

    check_tote = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related("closed_by")
        .filter(
            pk=check_tote_id,
            handover_batch_id=batch_id,
            status=FbsControllerCheckTote.STATUS_CLOSED,
            closed_by__isnull=False,
            closed_at__isnull=False,
        )
        .first()
    )
    if check_tote is None:
        return None

    from .handover import dispatch_handover_batch

    return dispatch_handover_batch(
        batch_id=batch_id,
        dispatched_by=check_tote.closed_by,
        verified_controller_auto=True,
        controller_check_tote_id=check_tote.id,
    )


def auto_dispatch_verified_wb_handovers(
    *,
    profile_ids: list[int] | tuple[int, ...] | None = None,
    limit: int = 100,
) -> tuple[int, ...]:
    """Retry marked WB auto-dispatches without touching legacy closed batches."""
    commands = (
        FbsMarketplaceCommand.objects.filter(
            command_type=WB_DELIVER_HANDOVER,
            handover_batch__status=FbsHandoverBatch.STATUS_READY,
            handover_batch__marketplace_state=(
                FbsHandoverBatch.MARKETPLACE_COMPLETE
            ),
        )
        .select_related("handover_batch")
        .order_by("-updated_at", "-id")
    )
    if profile_ids is not None:
        commands = commands.filter(profile_id__in=profile_ids)
    candidate_batch_ids: list[int] = []
    for command in commands[: max(int(limit), 1) * 4]:
        if _controller_auto_dispatch_check_tote_id(command) is None:
            continue
        if command.handover_batch_id not in candidate_batch_ids:
            candidate_batch_ids.append(command.handover_batch_id)
        if len(candidate_batch_ids) >= max(int(limit), 1):
            break

    dispatched_batch_ids: list[int] = []
    for candidate_batch_id in candidate_batch_ids:
        try:
            batch = auto_dispatch_verified_wb_handover(
                batch_id=candidate_batch_id
            )
        except FbsError as exc:
            logger.warning(
                "Unable to auto-dispatch verified WB handover %s: %s",
                candidate_batch_id,
                exc,
            )
            continue
        except Exception:
            logger.exception(
                "Unexpected failure auto-dispatching verified WB handover %s",
                candidate_batch_id,
            )
            continue
        if batch is not None and batch.status in {
            FbsHandoverBatch.STATUS_DISPATCHED,
            FbsHandoverBatch.STATUS_ACCEPTED,
        }:
            dispatched_batch_ids.append(batch.id)
    return tuple(dispatched_batch_ids)


def _wb_supply_lookup_candidates(
    supplies: list,
    *,
    intended_supply_id: str,
    include_completed: bool,
) -> list[dict]:
    candidates = []
    for row in supplies:
        if not isinstance(row, dict):
            continue
        supply_id = str(row.get("id") or "").strip()
        if not supply_id.startswith("WB-GI-") or supply_id == intended_supply_id:
            continue
        if not include_completed and bool(row.get("done")):
            continue
        candidates.append(row)
    candidates.sort(
        key=lambda row: (
            str(row.get("closedAt") or row.get("createdAt") or ""),
            str(row.get("id") or ""),
        ),
        reverse=True,
    )
    return candidates[:WB_SUPPLY_LOOKUP_MAX_CANDIDATES]


def _schedule_wb_order_supply_lookup(
    *,
    command: FbsMarketplaceCommand,
    write_command_id: int | None,
) -> FbsMarketplaceCommand | None:
    if not write_command_id or command.order is None or command.handover_batch is None:
        return None
    return _enqueue_handover_spec(
        command.handover_batch,
        build_wb_read_handover_supplies_spec(),
        order=command.order,
        idempotency_scope=f"locate-order-{write_command_id}-{command.id}",
        context={
            "readback_operation": WB_SUPPLY_LOOKUP_OPERATION,
            "write_command_id": write_command_id,
            "intended_supply_id": command.handover_batch.external_supply_id,
        },
    )


def _other_wb_supply_message(*, supply_id: str, supply_name: str = "") -> str:
    name = str(supply_name or "").strip()
    name_part = f" «{name}»" if name and name != supply_id else ""
    return (
        f"Не ОК: заказ уехал в другую поставку WB{name_part} ({supply_id}). "
        "В текущей поставке подтверждать его нельзя."
    )


def _finalize_wb_order_supply_lookup(command: FbsMarketplaceCommand) -> None:
    context = _handover_command_context(command)
    lookup_command_id = context.get("lookup_command_id")
    write_command_id = context.get("write_command_id")
    if not lookup_command_id or not write_command_id or command.order is None:
        return
    lookup_command = (
        FbsMarketplaceCommand.objects.select_for_update(of=("self",))
        .filter(pk=lookup_command_id)
        .first()
    )
    if lookup_command is None:
        return
    candidates = FbsMarketplaceCommand.objects.filter(
        command_type=WB_READ_HANDOVER_ORDER_IDS,
        payload__context__readback_operation=WB_SUPPLY_LOOKUP_CANDIDATE_OPERATION,
        payload__context__lookup_command_id=lookup_command_id,
    )
    if candidates.filter(
        status__in={
            FbsMarketplaceCommand.STATUS_PENDING,
            FbsMarketplaceCommand.STATUS_SENT,
            FbsMarketplaceCommand.STATUS_RETRY,
        }
    ).exists():
        return
    if candidates.filter(
        status=FbsMarketplaceCommand.STATUS_CONFIRMED,
        response_payload__matched=True,
    ).exists():
        return
    if candidates.filter(
        status__in={
            FbsMarketplaceCommand.STATUS_FAILED,
            FbsMarketplaceCommand.STATUS_CONFLICT,
        }
    ).exists():
        _retry_uncertain_write(
            int(write_command_id),
            "Не удалось проверить часть других поставок WB; назначена безопасная повторная сверка.",
        )
        return
    if _wb_order_is_terminal(command.order):
        _fail_uncertain_write(
            int(write_command_id),
            "WB не добавил заказ: сборочное задание уже завершено, отменено "
            "или передано в доставку; в доступных поставках WB заказ не найден.",
        )
        return
    _retry_uncertain_write(
        int(write_command_id),
        "Заказ не найден в других открытых поставках WB; повторяем добавление в текущую поставку.",
    )


def _recovery_compatibility_key(
    *, batch: FbsHandoverBatch, assignment: FbsHandoverOrderAssignment
) -> str:
    from .shipment_policy import is_tote_shipment

    if is_tote_shipment(batch):
        # Readback recovery must not turn a cart-owned supply into a shared
        # workstation supply. Existing legacy recovery keeps its old policy.
        return batch.compatibility_key
    match = re.search(r":workstation:(\d+)", str(batch.compatibility_key or ""))
    workstation_id = int(match.group(1)) if match else None
    return wb_handover_compatibility_key(
        assignment.order,
        workstation_id=workstation_id,
    )


@transaction.atomic
def _recover_missing_wb_handover_supply(
    command_id: int,
    response: MarketplaceHttpResponse,
) -> FbsMarketplaceCommand:
    """Move live orders away from a WB supply confirmed missing by readback."""
    command = (
        FbsMarketplaceCommand.objects.select_for_update(of=("self",))
        .select_related("handover_batch", "order")
        .get(pk=command_id)
    )
    batch = FbsHandoverBatch.objects.select_for_update().get(
        pk=command.handover_batch_id
    )
    reason = (
        f"WB не видит поставку {batch.external_supply_id}; "
        "действующие заказы перенесены в новую поставку."
    )
    assignments = list(
        FbsHandoverOrderAssignment.objects.select_for_update(of=("self",))
        .select_related("order", "assigned_by")
        .filter(
            batch=batch,
            status__in=(
                FbsHandoverOrderAssignment.STATUS_PENDING,
                FbsHandoverOrderAssignment.STATUS_ERROR,
            ),
            order__internal_status=FbsOrder.STATUS_PICKED,
            order__handover_order__isnull=True,
        )
        .order_by("id")
    )
    replacement_batches: dict[str, FbsHandoverBatch] = {}
    moved_order_ids = []
    now = timezone.now()
    for assignment in assignments:
        compatibility_key = _recovery_compatibility_key(
            batch=batch,
            assignment=assignment,
        )
        replacement = replacement_batches.get(compatibility_key)
        if replacement is None:
            suffix = f"A{assignment.id}"
            replacement = FbsHandoverBatch.objects.create(
                profile=batch.profile,
                external_name=(
                    f"FULLBOX-{batch.profile_id}-{now:%Y%m%d-%H%M%S}-"
                    f"R{batch.id}-{suffix}"
                ),
                compatibility_key=compatibility_key,
                created_by=assignment.assigned_by,
            )
            replacement_batches[compatibility_key] = replacement
        assignment.batch = replacement
        assignment.status = FbsHandoverOrderAssignment.STATUS_PENDING
        assignment.error = ""
        assignment.confirmed_at = None
        assignment.save(
            update_fields=["batch", "status", "error", "confirmed_at", "updated_at"]
        )
        moved_order_ids.append(assignment.order_id)

    if moved_order_ids:
        FbsMarketplaceCommand.objects.filter(
            handover_batch=batch,
            order_id__in=moved_order_ids,
            status__in=(
                FbsMarketplaceCommand.STATUS_PENDING,
                FbsMarketplaceCommand.STATUS_SENT,
                FbsMarketplaceCommand.STATUS_RETRY,
                FbsMarketplaceCommand.STATUS_CONFLICT,
            ),
        ).exclude(pk=command.pk).update(
            status=FbsMarketplaceCommand.STATUS_CANCELLED,
            error=reason,
            next_attempt_at=None,
            updated_at=now,
        )

    FbsWorkstation.objects.filter(active_handover_box__batch=batch).update(
        active_handover_box=None,
        updated_at=now,
    )
    FbsHandoverBox.objects.filter(
        batch=batch,
        status=FbsHandoverBox.STATUS_OPEN,
    ).update(
        status=FbsHandoverBox.STATUS_PROBLEM,
        problem_reason=reason,
        updated_at=now,
    )
    batch.status = FbsHandoverBatch.STATUS_PROBLEM
    batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_ERROR
    payload = batch.marketplace_payload if isinstance(batch.marketplace_payload, dict) else {}
    batch.marketplace_payload = {
        **payload,
        "recovery": {
            "reason": "supply_not_found",
            "missing_supply_id": batch.external_supply_id,
            "replacement_batch_ids": sorted(
                replacement.id for replacement in replacement_batches.values()
            ),
        },
    }
    batch.save(
        update_fields=["status", "marketplace_state", "marketplace_payload", "updated_at"]
    )
    _confirm_command(
        command,
        http_status=response.status_code,
        response_payload={
            "missing": True,
            "missing_supply_id": batch.external_supply_id,
            "moved_order_ids": moved_order_ids,
            "replacement_batch_ids": sorted(
                replacement.id for replacement in replacement_batches.values()
            ),
        },
    )
    for replacement in replacement_batches.values():
        schedule_wb_handover_supply(batch_id=replacement.id)
    return command


def _confirm_uncertain_write(command_id: int | None) -> None:
    if not command_id:
        return
    write_command = FbsMarketplaceCommand.objects.select_for_update().filter(pk=command_id).first()
    if write_command is None or write_command.status != FbsMarketplaceCommand.STATUS_CONFLICT:
        return
    _confirm_command(
        write_command,
        http_status=write_command.http_status or 200,
        response_payload={"confirmed_by_readback": True},
    )


def _retry_uncertain_write(command_id: int | None, reason: str) -> None:
    if not command_id:
        return
    write_command = FbsMarketplaceCommand.objects.select_for_update().filter(pk=command_id).first()
    if write_command is None or write_command.status != FbsMarketplaceCommand.STATUS_CONFLICT:
        return
    _set_retry_or_failed(write_command, reason)
    write_command.save(
        update_fields=["status", "error", "next_attempt_at", "updated_at"]
    )
    _sync_handover_failure(write_command)


def _fail_uncertain_write(command_id: int | None, reason: str) -> None:
    if not command_id:
        return
    write_command = FbsMarketplaceCommand.objects.select_for_update().filter(pk=command_id).first()
    if write_command is None or write_command.status != FbsMarketplaceCommand.STATUS_CONFLICT:
        return
    write_command.status = FbsMarketplaceCommand.STATUS_FAILED
    write_command.error = str(reason or "Команда отклонена маркетплейсом.")[:2000]
    write_command.next_attempt_at = None
    write_command.save(
        update_fields=["status", "error", "next_attempt_at", "updated_at"]
    )
    _sync_handover_failure(write_command)


@transaction.atomic
def _mark_http_failure(
    command_id: int,
    response: MarketplaceHttpResponse,
) -> FbsMarketplaceCommand:
    command = FbsMarketplaceCommand.objects.select_for_update().get(pk=command_id)
    command.http_status = response.status_code
    payload = response.json_payload if isinstance(response.json_payload, (dict, list)) else {}
    command.response_payload = payload
    command.error = f"Маркетплейс вернул HTTP {response.status_code}."
    if _is_wb_metadata_command(command):
        _set_retry_or_failed(command, command.error)
    elif response.status_code == 409:
        command.status = FbsMarketplaceCommand.STATUS_CONFLICT
        command.next_attempt_at = None
    elif response.status_code in RETRYABLE_HTTP_STATUSES or response.status_code >= 500:
        _set_retry_or_failed(command, command.error)
    else:
        command.status = FbsMarketplaceCommand.STATUS_FAILED
        command.next_attempt_at = None
    command.save(
        update_fields=[
            "status",
            "http_status",
            "response_payload",
            "error",
            "next_attempt_at",
            "updated_at",
        ]
    )
    _sync_transfer_failure(command)
    _sync_handover_failure(command)
    return command


def _confirm_command(
    command: FbsMarketplaceCommand,
    *,
    http_status: int,
    response_payload: dict,
) -> None:
    command.status = FbsMarketplaceCommand.STATUS_CONFIRMED
    command.http_status = http_status
    command.response_payload = response_payload
    command.error = ""
    command.next_attempt_at = None
    command.confirmed_at = timezone.now()
    command.save(
        update_fields=[
            "status",
            "http_status",
            "response_payload",
            "error",
            "next_attempt_at",
            "confirmed_at",
            "updated_at",
        ]
    )


def _schedule_wb_handover_orders_after_commit(assignment_ids) -> None:
    """Keep follow-up command creation outside the response row-lock graph."""
    stable_assignment_ids = tuple(
        sorted({int(assignment_id) for assignment_id in assignment_ids})
    )
    if not stable_assignment_ids:
        return

    def schedule_pending_assignments() -> None:
        for assignment_id in stable_assignment_ids:
            try:
                schedule_wb_handover_order(assignment_id=assignment_id)
            except Exception:
                # The periodic scheduler sees the still-pending assignment and
                # retries it. A follow-up enqueue must not undo the marketplace
                # response transaction that has already committed successfully.
                logger.exception(
                    "Deferred WB handover order scheduling failed "
                    "assignment_id=%s; periodic scheduler will retry",
                    assignment_id,
                )

    transaction.on_commit(schedule_pending_assignments, robust=True)


def _reconcile_wb_handover_composition(
    *, batch_id: int, order_ids: set[str]
) -> dict[str, int]:
    """Reconcile only settled assignments against the factual WB supply."""
    now = timezone.now()
    assignments = list(
        FbsHandoverOrderAssignment.objects.select_for_update(of=("self",))
        .select_related("order")
        .filter(
            batch_id=batch_id,
            status__in={
                FbsHandoverOrderAssignment.STATUS_CONFIRMED,
                FbsHandoverOrderAssignment.STATUS_ERROR,
            },
        )
        .order_by("id")
    )
    result = {"confirmed": 0, "missing": 0, "unchanged": 0}
    for assignment in assignments:
        external_order_id = str(assignment.order.external_order_id)
        if external_order_id in order_ids:
            changed = (
                assignment.status != FbsHandoverOrderAssignment.STATUS_CONFIRMED
                or bool(assignment.error)
                or assignment.confirmed_at is None
            )
            assignment.status = FbsHandoverOrderAssignment.STATUS_CONFIRMED
            assignment.error = ""
            assignment.confirmed_at = assignment.confirmed_at or now
            result["confirmed"] += 1
        else:
            error = (
                "Заказ отсутствует в фактическом составе WB поставки. "
                "Требуется возврат отбора или отдельная повторная подача "
                "активного задания."
            )
            changed = (
                assignment.status != FbsHandoverOrderAssignment.STATUS_ERROR
                or assignment.error != error
                or assignment.confirmed_at is not None
            )
            assignment.status = FbsHandoverOrderAssignment.STATUS_ERROR
            assignment.error = error
            assignment.confirmed_at = None
            result["missing"] += 1
        if changed:
            assignment.save(
                update_fields=["status", "error", "confirmed_at", "updated_at"]
            )
        else:
            result["unchanged"] += 1
    return result


@transaction.atomic
def _apply_success(
    command_id: int,
    response: MarketplaceHttpResponse,
) -> FbsMarketplaceCommand:
    command = (
        FbsMarketplaceCommand.objects.select_for_update(of=("self",))
        .select_related(
            "order__profile",
            "profile",
            "handover_batch",
            "handover_box",
        )
        .get(pk=command_id)
    )
    order = command.order
    if order is not None:
        order = (
            FbsOrder.objects.select_for_update(of=("self",))
            .select_related("profile")
            .get(pk=order.pk)
        )
        command.order = order

    if command.command_type == WB_CREATE_HANDOVER_SUPPLY:
        batch = FbsHandoverBatch.objects.select_for_update().get(pk=command.handover_batch_id)
        supply_id = parse_wb_supply_id(response.json_payload)
        if batch.external_supply_id and batch.external_supply_id != supply_id:
            raise FbsIntegrationError("Для поставки Fullbox уже сохранен другой ID WB.")
        batch.external_supply_id = supply_id
        batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_OPEN
        batch.marketplace_payload = {"id": supply_id, "name": batch.external_name}
        batch.save(
            update_fields=[
                "external_supply_id",
                "marketplace_state",
                "marketplace_payload",
                "updated_at",
            ]
        )
        _confirm_command(command, http_status=response.status_code, response_payload={"id": supply_id})
        assignment_ids = batch.order_assignments.filter(
            status=FbsHandoverOrderAssignment.STATUS_PENDING,
            order__internal_status=FbsOrder.STATUS_PICKED,
        ).values_list("id", flat=True)
        _schedule_wb_handover_orders_after_commit(assignment_ids)
        return command

    if command.command_type == WB_READ_HANDOVER_SUPPLIES:
        batch = command.handover_batch
        payload = response.json_payload if isinstance(response.json_payload, dict) else {}
        supplies = payload.get("supplies") if isinstance(payload.get("supplies"), list) else []
        context = _handover_command_context(command)
        if context.get("readback_operation") == WB_SUPPLY_LOOKUP_OPERATION:
            write_id = context.get("write_command_id")
            intended_supply_id = str(
                context.get("intended_supply_id") or batch.external_supply_id or ""
            ).strip()
            candidates = _wb_supply_lookup_candidates(
                supplies,
                intended_supply_id=intended_supply_id,
                include_completed=_wb_order_is_terminal(command.order),
            )
            for row in candidates:
                supply_id = parse_wb_supply_id(row)
                _enqueue_handover_spec(
                    batch,
                    build_wb_read_handover_order_ids_spec(supply_id),
                    order=command.order,
                    idempotency_scope=f"locate-order-{write_id}-{command.id}-{supply_id}",
                    context={
                        "readback_operation": WB_SUPPLY_LOOKUP_CANDIDATE_OPERATION,
                        "write_command_id": write_id,
                        "lookup_command_id": command.id,
                        "intended_supply_id": intended_supply_id,
                        "candidate_supply_id": supply_id,
                        "candidate_supply_name": str(row.get("name") or "").strip(),
                    },
                )
            _confirm_command(
                command,
                http_status=response.status_code,
                response_payload={
                    "candidate_count": len(candidates),
                    "candidate_supply_ids": [
                        str(row.get("id") or "").strip() for row in candidates
                    ],
                },
            )
            if not candidates:
                if _wb_order_is_terminal(command.order):
                    _fail_uncertain_write(
                        write_id,
                        "WB не добавил заказ: сборочное задание уже завершено, отменено "
                        "или передано в доставку; других подходящих поставок WB нет.",
                    )
                else:
                    _retry_uncertain_write(
                        write_id,
                        "Других открытых поставок WB нет; повторяем добавление в текущую поставку.",
                    )
            return command
        matches = [
            row for row in supplies
            if isinstance(row, dict) and str(row.get("name") or "").strip() == batch.external_name
        ]
        write_id = context.get("write_command_id")
        if len(matches) == 1:
            supply_id = parse_wb_supply_id(matches[0])
            batch.external_supply_id = supply_id
            batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_OPEN
            batch.marketplace_payload = matches[0]
            batch.save(
                update_fields=[
                    "external_supply_id",
                    "marketplace_state",
                    "marketplace_payload",
                    "updated_at",
                ]
            )
            _confirm_uncertain_write(write_id)
            assignment_ids = batch.order_assignments.filter(
                status=FbsHandoverOrderAssignment.STATUS_PENDING,
                order__internal_status=FbsOrder.STATUS_PICKED,
            ).values_list("id", flat=True)
            _schedule_wb_handover_orders_after_commit(assignment_ids)
        elif not matches:
            _retry_uncertain_write(write_id, "WB подтвердил отсутствие поставки; разрешен повтор.")
        else:
            batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_ERROR
            batch.save(update_fields=["marketplace_state", "updated_at"])
            raise FbsIntegrationError("WB вернул несколько поставок с одним техническим именем.")
        _confirm_command(command, http_status=response.status_code, response_payload={"matches": len(matches)})
        return command

    if command.command_type == WB_ADD_ORDER_TO_HANDOVER:
        return _defer_wb_handover_write_to_readback(
            command.id,
            "WB принял добавление заказа; сверяем фактический состав поставки.",
            response,
            expected_status=FbsMarketplaceCommand.STATUS_SENT,
        )

    if command.command_type == WB_CANCEL_ORDER:
        return _defer_wb_handover_write_to_readback(
            command.id,
            "WB принял отмену заказа; сверяем фактический состав поставки.",
            response,
            expected_status=FbsMarketplaceCommand.STATUS_SENT,
        )

    if command.command_type == WB_READ_HANDOVER_ORDER_IDS:
        order_ids = set(parse_wb_supply_order_ids(response.json_payload))
        context = _handover_command_context(command)
        write_id = context.get("write_command_id")
        if context.get("readback_operation") == WB_COMPOSITION_SYNC_OPERATION:
            summary = _reconcile_wb_handover_composition(
                batch_id=command.handover_batch_id,
                order_ids=order_ids,
            )
            _confirm_command(
                command,
                http_status=response.status_code,
                response_payload={
                    "order_ids": sorted(order_ids),
                    "reconciliation": summary,
                },
            )
            return command
        if context.get("readback_operation") == WB_SUPPLY_LOOKUP_CANDIDATE_OPERATION:
            supply_id = str(context.get("candidate_supply_id") or "").strip()
            matched = str(order.external_order_id) in order_ids
            _confirm_command(
                command,
                http_status=response.status_code,
                response_payload={
                    "matched": matched,
                    "order_id": str(order.external_order_id),
                    "supply_id": supply_id,
                },
            )
            if matched:
                _fail_uncertain_write(
                    write_id,
                    _other_wb_supply_message(
                        supply_id=supply_id,
                        supply_name=str(context.get("candidate_supply_name") or ""),
                    ),
                )
                FbsMarketplaceCommand.objects.filter(
                    command_type=WB_READ_HANDOVER_ORDER_IDS,
                    payload__context__readback_operation=WB_SUPPLY_LOOKUP_CANDIDATE_OPERATION,
                    payload__context__lookup_command_id=context.get("lookup_command_id"),
                    status__in={
                        FbsMarketplaceCommand.STATUS_PENDING,
                        FbsMarketplaceCommand.STATUS_RETRY,
                    },
                ).exclude(pk=command.pk).update(
                    status=FbsMarketplaceCommand.STATUS_CANCELLED,
                    error=f"Сверка завершена: заказ найден в поставке {supply_id}.",
                    next_attempt_at=None,
                    updated_at=timezone.now(),
                )
                return command
            _finalize_wb_order_supply_lookup(command)
            return command
        if context.get("readback_operation") == "exclude_order_supply_absence":
            restock_request_id = context.get("restock_request_id")
            if not restock_request_id:
                raise FbsIntegrationError("Команда исключения потеряла идентификатор возврата.")
            restock_request = (
                FbsPickRestockRequest.objects.select_related("order__profile")
                .filter(
                    pk=int(restock_request_id),
                    order_id=order.id,
                    handover_assignment__batch_id=command.handover_batch_id,
                )
                .first()
            )
            if restock_request is None:
                raise FbsIntegrationError(
                    "Команда исключения не соответствует возврату этого заказа."
                )
            from .pick_restock import (
                order_is_client_canceled_by_marketplace,
                queue_order_pick_restock_after_marketplace,
            )

            order_still_assigned = str(order.external_order_id) in order_ids
            client_cancellation_confirmed = bool(
                restock_request.reason_code
                == FbsPickRestockRequest.REASON_CLIENT_CANCELED
                and restock_request.marketplace_action
                == FbsPickRestockRequest.MARKETPLACE_ACTION_VERIFY_CANCEL
                and order_is_client_canceled_by_marketplace(restock_request.order)
            )
            if order_still_assigned and not client_cancellation_confirmed:
                return _mark_retry(
                    command.id,
                    "WB пока показывает заказ в поставке; повторяем безопасную сверку.",
                    expected_status=FbsMarketplaceCommand.STATUS_SENT,
                )

            queue_order_pick_restock_after_marketplace(
                request_id=int(restock_request_id)
            )
            _confirm_uncertain_write(write_id)
            _confirm_command(
                command,
                http_status=response.status_code,
                response_payload={
                    "order_ids": sorted(order_ids),
                    "excluded_order_id": str(order.external_order_id),
                    "restock_request_id": int(restock_request_id),
                    "order_still_assigned": order_still_assigned,
                    "client_cancellation_confirmed": client_cancellation_confirmed,
                },
            )
            return command
        if str(order.external_order_id) in order_ids:
            assignment = FbsHandoverOrderAssignment.objects.select_for_update().get(
                batch_id=command.handover_batch_id,
                order=order,
            )
            assignment.status = FbsHandoverOrderAssignment.STATUS_CONFIRMED
            assignment.error = ""
            assignment.confirmed_at = timezone.now()
            assignment.save(update_fields=["status", "error", "confirmed_at", "updated_at"])
            order.marketplace_status = "confirm"
            order.save(update_fields=["marketplace_status", "updated_at"])
            _confirm_uncertain_write(write_id)
            from .pick_restock import (
                finalize_invalid_kiz_reroute,
                invalid_kiz_reroute_context,
            )

            if invalid_kiz_reroute_context(assignment.batch) is not None:
                finalize_invalid_kiz_reroute(assignment_id=assignment.id)
            else:
                schedule_marketplace_metadata(order_id=order.id)
                schedule_label_preparation(order_id=order.id)
            _schedule_controller_wb_box_if_ready(batch_id=assignment.batch_id)
        else:
            lookup_command = _schedule_wb_order_supply_lookup(
                command=command,
                write_command_id=write_id,
            )
            if lookup_command is None:
                _retry_uncertain_write(
                    write_id,
                    "Не удалось запустить сверку поставок WB; повторяем добавление безопасно.",
                )
        _confirm_command(
            command,
            http_status=response.status_code,
            response_payload={"order_ids": sorted(order_ids)},
        )
        return command

    if command.command_type == WB_CREATE_HANDOVER_BOXES:
        batch = FbsHandoverBatch.objects.select_for_update().get(pk=command.handover_batch_id)
        box_ids = parse_wb_created_handover_box_ids(response.json_payload)
        expected_amount = int(_handover_command_context(command).get("amount") or 0)
        if expected_amount and len(box_ids) != expected_amount:
            raise FbsIntegrationError("WB вернул не все ID созданных транспортных коробов.")
        for external_box_id in box_ids:
            box, _ = FbsHandoverBox.objects.get_or_create(
                batch=batch,
                external_box_id=external_box_id,
                defaults={"qr_code": f"PENDING:{external_box_id}"},
            )
            _enqueue_handover_spec(
                batch,
                build_wb_handover_box_label_spec(
                    supply_id=batch.external_supply_id,
                    external_box_id=external_box_id,
                ),
                box=box,
                idempotency_scope=f"box-label-{external_box_id}",
            )
        _confirm_command(
            command,
            http_status=response.status_code,
            response_payload={"trbx_ids": list(box_ids)},
        )
        return command

    if command.command_type == WB_READ_HANDOVER_BOXES:
        batch = command.handover_batch
        remote_ids = set(parse_wb_handover_box_ids(response.json_payload))
        context = _handover_command_context(command)
        known_ids = set(context.get("known_ids") or [])
        write_id = context.get("write_command_id")
        if write_id:
            write_command = FbsMarketplaceCommand.objects.select_for_update().get(pk=write_id)
            write_context = _handover_command_context(write_command)
            known_ids = set(write_context.get("known_ids") or [])
            expected_amount = int(write_context.get("amount") or 0)
            new_ids = sorted(remote_ids - known_ids)
            if len(new_ids) == expected_amount:
                for external_box_id in new_ids:
                    box, _ = FbsHandoverBox.objects.get_or_create(
                        batch=batch,
                        external_box_id=external_box_id,
                        defaults={"qr_code": f"PENDING:{external_box_id}"},
                    )
                    _enqueue_handover_spec(
                        batch,
                        build_wb_handover_box_label_spec(
                            supply_id=batch.external_supply_id,
                            external_box_id=external_box_id,
                        ),
                        box=box,
                        idempotency_scope=f"box-label-{external_box_id}",
                    )
                _confirm_uncertain_write(write_id)
            elif not new_ids:
                _retry_uncertain_write(write_id, "WB подтвердил отсутствие новых коробов; разрешен повтор.")
            else:
                raise FbsIntegrationError("Нельзя однозначно сопоставить созданные WB короба.")
        _confirm_command(
            command,
            http_status=response.status_code,
            response_payload={"trbx_ids": sorted(remote_ids)},
        )
        return command

    if command.command_type == WB_FETCH_HANDOVER_BOX_LABEL:
        box = FbsHandoverBox.objects.select_for_update().get(pk=command.handover_box_id)
        barcode, content = parse_wb_png_label(response.json_payload, source="box")
        content_hash = hashlib.sha256(content).hexdigest()
        if box.label_file and box.label_hash != content_hash:
            raise FbsIntegrationError("WB повторно вернул другой файл QR короба.")
        box.qr_code = barcode
        box.label_format = FbsOrderLabel.FORMAT_PNG
        box.label_hash = content_hash
        box.label_ready_at = timezone.now()
        if not box.label_file:
            box.label_file.save(
                f"wb-box-{box.external_box_id}.png",
                ContentFile(content),
                save=False,
            )
        box.save(
            update_fields=[
                "qr_code",
                "label_file",
                "label_format",
                "label_hash",
                "label_ready_at",
                "updated_at",
            ]
        )
        _confirm_command(
            command,
            http_status=response.status_code,
            response_payload={"barcode": barcode, "sha256": content_hash},
        )
        return command

    if command.command_type == WB_DELIVER_HANDOVER:
        batch = FbsHandoverBatch.objects.select_for_update().get(pk=command.handover_batch_id)
        batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_COMPLETE
        batch.save(update_fields=["marketplace_state", "updated_at"])
        _confirm_command(command, http_status=response.status_code, response_payload={"done": True})
        _enqueue_handover_spec(
            batch,
            build_wb_handover_supply_label_spec(batch.external_supply_id),
            idempotency_scope="supply-label",
        )
        try:
            auto_dispatch_verified_wb_handover(batch_id=batch.id)
        except FbsError as exc:
            logger.warning(
                "WB delivery confirmed but controller auto-dispatch for batch %s is waiting: %s",
                batch.id,
                exc,
            )
        return command

    if command.command_type == WB_READ_HANDOVER_SUPPLY:
        batch = command.handover_batch
        payload = response.json_payload if isinstance(response.json_payload, dict) else {}
        done = bool(payload.get("done"))
        context = _handover_command_context(command)
        write_id = context.get("write_command_id")
        if context.get("readback_operation") == "add_order_supply_presence":
            if done:
                batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_COMPLETE
                batch.marketplace_payload = payload
                batch.save(
                    update_fields=[
                        "marketplace_state",
                        "marketplace_payload",
                        "updated_at",
                    ]
                )
                _fail_uncertain_write(
                    write_id,
                    "WB подтвердил, что поставка уже закрыта. Заказ не добавлен; "
                    "создайте новый поток отгрузки.",
                )
            else:
                _enqueue_handover_spec(
                    batch,
                    build_wb_read_handover_order_ids_spec(batch.external_supply_id),
                    order=command.order,
                    idempotency_scope=f"order-presence-{write_id}-{command.id}",
                    context={
                        "write_command_id": write_id,
                        "readback_operation": "add_order_target_membership",
                        "intended_supply_id": batch.external_supply_id,
                    },
                )
            _confirm_command(
                command,
                http_status=response.status_code,
                response_payload=payload,
            )
            return command
        if done:
            batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_COMPLETE
            batch.marketplace_payload = payload
            batch.save(
                update_fields=["marketplace_state", "marketplace_payload", "updated_at"]
            )
            _confirm_uncertain_write(write_id)
            _enqueue_handover_spec(
                batch,
                build_wb_handover_supply_label_spec(batch.external_supply_id),
                idempotency_scope="supply-label",
            )
            try:
                auto_dispatch_verified_wb_handover(batch_id=batch.id)
            except FbsError as exc:
                logger.warning(
                    "WB delivery readback confirmed but controller auto-dispatch for batch %s is waiting: %s",
                    batch.id,
                    exc,
                )
        else:
            _retry_uncertain_write(write_id, "WB подтвердил открытую поставку; разрешен повтор deliver.")
        _confirm_command(command, http_status=response.status_code, response_payload=payload)
        return command

    if command.command_type == WB_FETCH_HANDOVER_SUPPLY_LABEL:
        batch = FbsHandoverBatch.objects.select_for_update().get(pk=command.handover_batch_id)
        barcode, content = parse_wb_png_label(response.json_payload, source="supply")
        content_hash = hashlib.sha256(content).hexdigest()
        if batch.supply_label_file and batch.supply_label_hash != content_hash:
            raise FbsIntegrationError("WB повторно вернул другой файл QR поставки.")
        batch.supply_qr_code = barcode
        batch.supply_label_format = FbsOrderLabel.FORMAT_PNG
        batch.supply_label_hash = content_hash
        if not batch.supply_label_file:
            batch.supply_label_file.save(
                f"wb-supply-{batch.external_supply_id}.png",
                ContentFile(content),
                save=False,
            )
        batch.save(
            update_fields=[
                "supply_qr_code",
                "supply_label_file",
                "supply_label_format",
                "supply_label_hash",
                "updated_at",
            ]
        )
        _confirm_command(
            command,
            http_status=response.status_code,
            response_payload={"barcode": barcode, "sha256": content_hash},
        )
        return command

    if order is None:
        raise FbsIntegrationError("Команда не связана с заказом FBS.")

    if command.command_type in WB_METADATA_WRITE_COMMANDS:
        metadata = {
            "order_id": order.external_order_id,
            "metadata_type": command.command_type,
            "write_accepted": True,
        }
        _confirm_command(command, http_status=response.status_code, response_payload=metadata)
        _enqueue_spec(
            order,
            build_wb_metadata_readback_spec(order),
            idempotency_scope=f"after-{command.id}",
        )
        return command

    if command.command_type == WB_READ_ORDER_METADATA:
        metadata = parse_wb_metadata_response(order, response)
        return _apply_wb_metadata_readback(command, metadata, response)

    if command.command_type == OZON_CREATE_OR_GET_EXEMPLARS:
        metadata = parse_ozon_create_exemplars_response(order, response)
        body = _prepare_ozon_set_body(order, metadata)
        _confirm_command(command, http_status=response.status_code, response_payload=metadata)
        if body is None:
            schedule_label_preparation(order_id=order.id)
            return command
        spec = build_ozon_set_exemplars_spec(order, body)
        marking = list(
            FbsMarketplaceMetadataTransfer.objects.filter(
                order_item__order=order,
                metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
            ).exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED)
        )
        _enqueue_spec(order, spec, idempotency_scope=_command_scope(marking))
        return command

    if command.command_type == OZON_SET_EXEMPLARS:
        metadata = parse_ozon_set_exemplars_response(order, response)
        _confirm_command(command, http_status=response.status_code, response_payload=metadata)
        _set_transfer_state(
            list(
                _metadata_transfers_for_command(command)
                .select_for_update()
                .exclude(status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED)
            ),
            status=FbsMarketplaceMetadataTransfer.STATUS_SENT,
            external_status="ozon_validation_task_created",
        )
        _enqueue_spec(
            order,
            build_ozon_exemplar_status_spec(order),
            idempotency_scope=f"after-{command.id}",
        )
        return command

    if command.command_type == OZON_READ_EXEMPLAR_STATUS:
        metadata = parse_ozon_exemplar_status_response(order, response)
        return _apply_ozon_exemplar_status(command, metadata, response)

    if command.command_type == WB_FETCH_ORDER_STICKER:
        parsed = parse_wb_sticker_response(order, response)
        register_marketplace_label(
            order_id=order.id,
            external_label_id=parsed.external_label_id,
            barcode=parsed.barcode,
            label_format=parsed.label_format,
            payload=parsed.metadata,
            file_name=parsed.file_name,
            file_content=parsed.content,
        )
        _confirm_command(command, http_status=response.status_code, response_payload=parsed.metadata)
        return command

    if command.command_type == OZON_SHIP_POSTING:
        metadata = parse_ozon_ship_response(order, response)
        _confirm_command(command, http_status=response.status_code, response_payload=metadata)
        _enqueue_spec(
            order,
            build_ozon_readback_spec(order),
            idempotency_scope=f"ship-attempt-{command.attempt_count}",
        )
        return command

    if command.command_type == OZON_READ_POSTING_AFTER_SHIP:
        metadata = parse_ozon_readback_response(order, response)
        marketplace_status = metadata["status"].lower()
        order.marketplace_status = marketplace_status
        order.marketplace_substatus = metadata["substatus"]
        order_raw_payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
        order.raw_payload = dict(order_raw_payload)
        if metadata["barcodes"]:
            order.raw_payload["barcodes"] = metadata["barcodes"]
        order.save(
            update_fields=[
                "marketplace_status",
                "marketplace_substatus",
                "raw_payload",
                "updated_at",
            ]
        )
        metadata["status"] = marketplace_status
        if marketplace_status in MARKETPLACE_PROBLEM_STATUSES:
            _confirm_command(
                command,
                http_status=response.status_code,
                response_payload=metadata,
            )
            return command
        if marketplace_status in OZON_ACCEPTED_STATUSES:
            try:
                label = confirm_preloaded_ozon_order_label_after_marketplace_advance(
                    order_id=order.id,
                    barcodes=metadata["barcodes"],
                    marketplace_status=marketplace_status,
                )
            except FbsLabelError as exc:
                label = None
                late_status_error = str(exc)
            else:
                late_status_error = (
                    "Ozon уже перевел заказ в доставку, но подтвержденный QR "
                    "заказа в Fullbox не найден."
                )
            if label is None:
                command.status = FbsMarketplaceCommand.STATUS_CONFLICT
                command.http_status = response.status_code
                command.response_payload = metadata
                command.error = late_status_error
                command.next_attempt_at = None
                command.save(
                    update_fields=[
                        "status",
                        "http_status",
                        "response_payload",
                        "error",
                        "next_attempt_at",
                        "updated_at",
                    ]
                )
                return command
            _confirm_command(
                command,
                http_status=response.status_code,
                response_payload=metadata,
            )
            return command
        if marketplace_status == OZON_AWAITING_PACKAGING:
            ship_command = (
                FbsMarketplaceCommand.objects.select_for_update()
                .filter(
                    order=order,
                    command_type=OZON_SHIP_POSTING,
                    status=FbsMarketplaceCommand.STATUS_CONFLICT,
                )
                .order_by("id")
                .first()
            )
            if ship_command is not None:
                _set_retry_or_failed(
                    ship_command,
                    "Ozon подтвердил, что отправление еще ожидает сборку.",
                )
                ship_command.save(
                    update_fields=["status", "error", "next_attempt_at", "updated_at"]
                )
                _confirm_command(
                    command,
                    http_status=response.status_code,
                    response_payload=metadata,
                )
                return command
        if marketplace_status != OZON_AWAITING_DELIVER:
            _set_retry_or_failed(
                command,
                "Ozon ещё не подтвердил статус awaiting_deliver; назначена повторная сверка.",
            )
            command.http_status = response.status_code
            command.response_payload = metadata
            command.save(
                update_fields=[
                    "status",
                    "http_status",
                    "response_payload",
                    "error",
                    "next_attempt_at",
                    "updated_at",
                ]
            )
            return command
        _confirm_command(command, http_status=response.status_code, response_payload=metadata)
        _enqueue_spec(order, build_ozon_label_spec(order))
        return command

    if command.command_type == OZON_FETCH_POSTING_LABEL:
        parsed = parse_ozon_label_response(order, response)
        register_marketplace_label(
            order_id=order.id,
            external_label_id=parsed.external_label_id,
            barcode=parsed.barcode,
            label_format=parsed.label_format,
            payload=parsed.metadata,
            file_name=parsed.file_name,
            file_content=parsed.content,
        )
        _confirm_command(command, http_status=response.status_code, response_payload=parsed.metadata)
        return command

    raise FbsIntegrationError("Тип команды FBS не поддерживается диспетчером.")


def _apply_success_with_deadlock_retry(
    command_id: int,
    response: MarketplaceHttpResponse,
) -> FbsMarketplaceCommand:
    """Replay only a fully rolled-back local confirmation transaction."""
    for attempt in range(MARKETPLACE_DEADLOCK_RETRY_ATTEMPTS):
        try:
            return _apply_success(command_id, response)
        except OperationalError as exc:
            if (
                not _is_database_deadlock(exc)
                or attempt + 1 >= MARKETPLACE_DEADLOCK_RETRY_ATTEMPTS
            ):
                raise
            logger.warning(
                "FBS marketplace confirmation deadlock; retrying command=%s attempt=%s",
                command_id,
                attempt + 2,
            )
            time.sleep(MARKETPLACE_DEADLOCK_RETRY_DELAY_SECONDS * (attempt + 1))
    raise AssertionError("unreachable")


def process_marketplace_command(
    *,
    command_id: int,
    transport: MarketplaceTransport | None = None,
) -> FbsMarketplaceCommand:
    _require_outbox()
    with transaction.atomic():
        command = (
            FbsMarketplaceCommand.objects.select_for_update(of=("self",))
            .select_related("profile", "order")
            .get(pk=command_id)
        )
        if command.status not in ACTIVE_COMMAND_STATUSES:
            return command
        if _cancel_excluded_wb_add_command(command):
            return command
        if (
            command.command_type == WB_ADD_ORDER_TO_HANDOVER
            and _wb_order_is_terminal(command.order)
        ):
            command.status = FbsMarketplaceCommand.STATUS_FAILED
            command.error = (
                "WB не добавил заказ: сборочное задание уже отменено, "
                "завершено или передано в доставку."
            )
            command.next_attempt_at = None
            command.save(
                update_fields=["status", "error", "next_attempt_at", "updated_at"]
            )
            _sync_handover_failure(command)
            return command
        if command.next_attempt_at and command.next_attempt_at > timezone.now():
            return command
        if not command.profile.is_active or not command.profile.outbox_enabled:
            # The profile may have been disabled after the worker selected it.
            # Leave its command intact and let other profiles keep progressing.
            return command
        if command.command_type in METADATA_COMMAND_TYPES and (
            not feature_enabled("marking_push") or not command.profile.marking_push_enabled
        ):
            raise FbsFeatureDisabled("Передача КИЗов и метаданных FBS выключена.")
        command.status = FbsMarketplaceCommand.STATUS_SENT
        command.attempt_count += 1
        command.error = ""
        command.save(update_fields=["status", "attempt_count", "error", "updated_at"])
        _mark_command_transfers_sent(command)

    sender = transport or RequestsMarketplaceTransport()
    try:
        response = sender.send(command)
    except FbsIntegrationError as exc:
        if command.command_type in WB_HANDOVER_WRITE_COMMANDS:
            return _defer_wb_handover_write_to_readback(command.id, str(exc))
        if command.command_type == OZON_SHIP_POSTING:
            return _defer_ozon_ship_to_readback(command.id, str(exc))
        if command.command_type in METADATA_WRITE_COMMANDS:
            return _defer_metadata_write_to_readback(command.id, str(exc))
        return _mark_retry(command.id, str(exc))
    except Exception:
        logger.exception("Unexpected FBS marketplace transport error")
        if command.command_type in WB_HANDOVER_WRITE_COMMANDS:
            return _defer_wb_handover_write_to_readback(
                command.id,
                "Результат команды поставки WB не определен; назначена сверка.",
            )
        if command.command_type == OZON_SHIP_POSTING:
            return _defer_ozon_ship_to_readback(
                command.id,
                "Результат отправки Ozon не определен; назначена сверка.",
            )
        if command.command_type in METADATA_WRITE_COMMANDS:
            return _defer_metadata_write_to_readback(
                command.id,
                "Результат передачи метаданных не определен; назначена сверка.",
            )
        return _mark_retry(command.id, "Не удалось выполнить запрос к маркетплейсу.")
    if not 200 <= response.status_code < 300:
        if (
            command.command_type == WB_DELIVER_HANDOVER
            and response.status_code == 409
        ):
            marked = _mark_wb_deliver_metadata_rejections(command.id, response)
            if marked is not None:
                return marked
        if (
            command.command_type == WB_READ_HANDOVER_SUPPLY
            and response.status_code == 404
            and _handover_command_context(command).get("readback_operation")
            == "add_order_supply_presence"
        ):
            return _recover_missing_wb_handover_supply(command.id, response)
        if command.command_type in WB_HANDOVER_WRITE_COMMANDS and (
            response.status_code == 409
            or response.status_code in RETRYABLE_HTTP_STATUSES
            or response.status_code >= 500
        ):
            return _defer_wb_handover_write_to_readback(
                command.id,
                "Результат команды поставки WB не определен; назначена сверка.",
                response,
            )
        if command.command_type == OZON_SHIP_POSTING and (
            response.status_code == 409
            or response.status_code in RETRYABLE_HTTP_STATUSES
            or response.status_code >= 500
            or _ozon_ship_response_requires_readback(response)
        ):
            return _defer_ozon_ship_to_readback(
                command.id,
                "Результат отправки Ozon не определен; назначена сверка.",
                response,
            )
        if command.command_type in WB_METADATA_WRITE_COMMANDS:
            return _defer_metadata_write_to_readback(
                command.id,
                "WB не подтвердил результат передачи метаданных; назначена сверка.",
                response,
            )
        if command.command_type in OZON_METADATA_WRITE_COMMANDS and (
            response.status_code == 409
            or response.status_code in RETRYABLE_HTTP_STATUSES
            or response.status_code >= 500
        ):
            return _defer_metadata_write_to_readback(
                command.id,
                "Результат передачи метаданных не определен; назначена сверка.",
                response,
            )
        return _mark_http_failure(command.id, response)
    try:
        return _apply_success_with_deadlock_retry(command.id, response)
    except FbsError as exc:
        if command.command_type in WB_HANDOVER_WRITE_COMMANDS:
            return _defer_wb_handover_write_to_readback(command.id, str(exc), response)
        if command.command_type == OZON_SHIP_POSTING:
            return _defer_ozon_ship_to_readback(command.id, str(exc), response)
        if command.command_type in METADATA_WRITE_COMMANDS:
            return _defer_metadata_write_to_readback(command.id, str(exc), response)
        return _mark_retry(command.id, str(exc))
    except Exception:
        logger.exception("Unexpected FBS marketplace response processing error")
        if command.command_type in WB_HANDOVER_WRITE_COMMANDS:
            return _defer_wb_handover_write_to_readback(
                command.id,
                "Не удалось подтвердить ответ команды поставки WB; назначена сверка.",
                response,
            )
        if command.command_type == OZON_SHIP_POSTING:
            return _defer_ozon_ship_to_readback(
                command.id,
                "Не удалось подтвердить результат отправки Ozon; назначена сверка.",
                response,
            )
        if command.command_type in METADATA_WRITE_COMMANDS:
            return _defer_metadata_write_to_readback(
                command.id,
                "Не удалось подтвердить результат передачи метаданных; назначена сверка.",
                response,
            )
        return _mark_retry(command.id, "Не удалось обработать ответ маркетплейса.")


def _recover_stale_sent_commands(*, command_types: set[str] | None = None) -> None:
    timeout_seconds = max(
        int(getattr(settings, "FBS_MARKETPLACE_SENT_TIMEOUT_SECONDS", 300)),
        30,
    )
    cutoff = timezone.now() - timedelta(seconds=timeout_seconds)
    stale_queryset = FbsMarketplaceCommand.objects.filter(
        status=FbsMarketplaceCommand.STATUS_SENT,
        updated_at__lte=cutoff,
    )
    if command_types is not None:
        stale_queryset = stale_queryset.filter(command_type__in=command_types)
    stale_commands = list(stale_queryset.values_list("id", "command_type"))
    for command_id, command_type in stale_commands:
        if command_type in WB_HANDOVER_WRITE_COMMANDS:
            _defer_wb_handover_write_to_readback(
                command_id,
                "Команда поставки WB зависла; назначена сверка результата.",
                expected_status=FbsMarketplaceCommand.STATUS_SENT,
            )
        elif command_type == OZON_SHIP_POSTING:
            _defer_ozon_ship_to_readback(
                command_id,
                "Выполнение Ozon не завершилось; назначена сверка статуса.",
                expected_status=FbsMarketplaceCommand.STATUS_SENT,
            )
        elif command_type in METADATA_WRITE_COMMANDS:
            _defer_metadata_write_to_readback(
                command_id,
                "Команда передачи метаданных зависла; назначена сверка результата.",
                expected_status=FbsMarketplaceCommand.STATUS_SENT,
            )
        else:
            _mark_retry(
                command_id,
                "Команда не завершилась и возвращена в очередь.",
                expected_status=FbsMarketplaceCommand.STATUS_SENT,
            )


def _schedule_prepared_metadata_commands(limit: int) -> None:
    if not feature_enabled("marking_push"):
        return
    raw_order_ids = list(
        FbsMarketplaceMetadataTransfer.objects.filter(
            status=FbsMarketplaceMetadataTransfer.STATUS_PREPARED,
            order_item__order__internal_status=FbsOrder.STATUS_PICKED,
            order_item__order__profile__is_active=True,
            order_item__order__profile__outbox_enabled=True,
            order_item__order__profile__marking_push_enabled=True,
        )
        .order_by("prepared_at", "id")
        .values_list("order_item__order_id", flat=True)[: max(int(limit), 1) * 4]
    )
    order_ids = list(dict.fromkeys(raw_order_ids))[: max(int(limit), 1)]
    for order_id in order_ids:
        schedule_marketplace_metadata(order_id=order_id)


def _schedule_active_wb_verification_label_requests(limit: int) -> None:
    """Prepare WB labels as soon as each order is picked in an active wave.

    Starting the whole wave is too early: an order can still be short or
    canceled.  Once its pick task is complete, however, the existing
    wave-owned shipment can be prepared safely while the picker continues
    with the remaining orders.  The controller tote prefetch remains a
    fallback for transient failures and waves already at verification.
    """
    prepared_label = FbsOrderLabel.objects.filter(
        order_id=OuterRef("order_id"),
        status__in=(
            FbsOrderLabel.STATUS_REQUESTED,
            FbsOrderLabel.STATUS_READY,
            FbsOrderLabel.STATUS_APPLIED,
        ),
    )
    active_restock = FbsPickRestockRequest.objects.filter(
        order_id=OuterRef("order_id"),
    ).exclude(status=FbsPickRestockRequest.STATUS_CANCELED)
    tasks = list(
        FbsPickTask.objects.filter(
            status=FbsPickTask.STATUS_PICKED,
            order__internal_status=FbsOrder.STATUS_PICKED,
            order__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            order__profile__is_active=True,
            order__profile__outbox_enabled=True,
            batch__status__in=(
                FbsPickBatch.STATUS_IN_PROGRESS,
                FbsPickBatch.STATUS_VERIFICATION,
            ),
        )
        .annotate(
            _has_prepared_label=Exists(prepared_label),
            _has_active_restock=Exists(active_restock),
        )
        .filter(
            _has_prepared_label=False,
            _has_active_restock=False,
        )
        .select_related(
            "assigned_to",
            "batch__assigned_to",
            "batch__verification_assigned_to",
        )
        .order_by("batch__verification_started_at", "batch_id", "sort_order", "id")[
            : max(int(limit), 1)
        ]
    )
    for task in tasks:
        requested_by = (
            task.batch.verification_assigned_to
            or task.assigned_to
            or task.batch.assigned_to
        )
        try:
            prefetch_wb_order_label_request(
                order_id=task.order_id,
                requested_by=requested_by,
            )
        except (FbsError, OperationalError) as exc:
            # A busy or stale order must not hold the rest of the wave.  It
            # stays eligible and will be retried by the next fast worker pass.
            logger.info(
                "FBS WB background label request deferred order=%s error=%s",
                task.order_id,
                exc,
            )


def _schedule_verified_handover_assignments(limit: int) -> None:
    """Create shipment assignments for picked orders outside scan transactions."""
    labels = list(
        FbsOrderLabel.objects.filter(
            status=FbsOrderLabel.STATUS_REQUESTED,
            order__internal_status=FbsOrder.STATUS_PICKED,
            order__profile__is_active=True,
            order__profile__outbox_enabled=True,
            order__handover_assignment__isnull=True,
        )
        .select_related("order__profile", "requested_by")
        .order_by("requested_at", "id")[: max(int(limit), 1)]
    )
    if not labels:
        return

    from .handover import ensure_order_handover_assignment
    from .totes import controller_pick_tote_for_batch

    for label in labels:
        task = (
            FbsPickTask.objects.filter(
                order_id=label.order_id,
                status=FbsPickTask.STATUS_PICKED,
                batch__status__in=(
                    FbsPickBatch.STATUS_IN_PROGRESS,
                    FbsPickBatch.STATUS_VERIFICATION,
                ),
            )
            .select_related("batch")
            .order_by("-batch__verification_started_at", "-id")
            .first()
        )
        if task is None:
            continue
        pick_tote_context = controller_pick_tote_for_batch(batch_id=task.batch_id)
        try:
            ensure_order_handover_assignment(
                order_id=label.order_id,
                assigned_by=label.requested_by,
                workstation_id=task.batch.workstation_id,
                pick_batch_id=task.batch_id,
                check_tote_id=(
                    pick_tote_context.check_tote_id
                    if pick_tote_context is not None
                    else None
                ),
            )
        except FbsError as exc:
            # A single malformed/stale order must not stop metadata and labels
            # for the rest of the controller wave.  The durable requested
            # label keeps this order eligible for the next scheduler pass.
            logger.warning(
                "FBS background handover assignment deferred order=%s error=%s",
                label.order_id,
                exc,
            )


def _schedule_waiting_label_commands(limit: int, *, marketplace: str = "") -> None:
    labels = FbsOrderLabel.objects.filter(
        status=FbsOrderLabel.STATUS_REQUESTED,
        order__internal_status=FbsOrder.STATUS_PICKED,
        order__profile__is_active=True,
        order__profile__outbox_enabled=True,
    )
    if marketplace:
        labels = labels.filter(order__profile__marketplace=marketplace)
    labels = labels.filter(
        Q(order__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB)
        | Q(order__handover_assignment__isnull=False)
    )
    order_ids = list(
        labels
        .order_by("requested_at", "id")
        .values_list("order_id", flat=True)[: max(int(limit), 1)]
    )
    for order_id in order_ids:
        schedule_label_preparation(order_id=order_id)


def _schedule_pending_handover_commands(limit: int) -> None:
    assignment_ids = list(
        FbsHandoverOrderAssignment.objects.filter(
            status=FbsHandoverOrderAssignment.STATUS_PENDING,
            order__internal_status=FbsOrder.STATUS_PICKED,
            batch__status=FbsHandoverBatch.STATUS_OPEN,
            batch__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            batch__profile__is_active=True,
            batch__profile__outbox_enabled=True,
        )
        .order_by("created_at", "id")
        .values_list("id", flat=True)[: max(int(limit), 1)]
    )
    for assignment_id in assignment_ids:
        schedule_wb_handover_order(assignment_id=assignment_id)


def _schedule_open_wb_handover_readbacks(limit: int) -> None:
    now = timezone.now()
    recent_batch_ids = FbsMarketplaceCommand.objects.filter(
        command_type=WB_READ_HANDOVER_ORDER_IDS,
        idempotency_key__contains=":composition-sync-",
        handover_batch_id__isnull=False,
    ).filter(
        Q(created_at__gte=now - WB_COMPOSITION_SYNC_INTERVAL)
        | Q(status__in=ACTIVE_COMMAND_STATUSES)
    ).values_list("handover_batch_id", flat=True)
    batch_ids = list(
        FbsHandoverBatch.objects.filter(
            status__in={
                FbsHandoverBatch.STATUS_OPEN,
                FbsHandoverBatch.STATUS_READY,
            },
            profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            profile__is_active=True,
            profile__outbox_enabled=True,
            external_supply_id__startswith="WB-GI-",
            order_assignments__status__in={
                FbsHandoverOrderAssignment.STATUS_CONFIRMED,
                FbsHandoverOrderAssignment.STATUS_ERROR,
            },
        )
        .exclude(pk__in=recent_batch_ids)
        .order_by("updated_at", "id")
        .values_list("id", flat=True)
        .distinct()[: min(max(int(limit), 1), 10)]
    )
    for batch_id in batch_ids:
        schedule_wb_handover_composition_sync(batch_id=batch_id)


def _recover_ozon_semantic_ship_failures(limit: int) -> None:
    """Reconcile older Ozon ship failures that were stored before this fix."""
    candidates = list(
        FbsMarketplaceCommand.objects.filter(
            command_type=OZON_SHIP_POSTING,
            status=FbsMarketplaceCommand.STATUS_FAILED,
            http_status=400,
            profile__marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            profile__is_active=True,
            profile__outbox_enabled=True,
            order__isnull=False,
        )
        .order_by("updated_at", "id")[: max(int(limit), 1) * 4]
    )
    recovered = 0
    for command in candidates:
        response = MarketplaceHttpResponse(
            status_code=int(command.http_status or 400),
            headers={},
            content=b"",
            json_payload=command.response_payload,
        )
        if not _ozon_ship_response_requires_readback(response):
            continue
        _defer_ozon_ship_to_readback(
            command.id,
            "Сохраненный ответ Ozon требует сверки актуального статуса заказа.",
            response,
            expected_status=FbsMarketplaceCommand.STATUS_FAILED,
        )
        recovered += 1
        if recovered >= max(int(limit), 1):
            break


def process_wb_label_queue(
    *,
    limit: int = 50,
    transport: MarketplaceTransport | None = None,
) -> MarketplaceQueueResult:
    """Fetch only requested WB order stickers; do not run other outbox commands."""
    _require_outbox()
    _schedule_waiting_label_commands(
        limit,
        marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
    )
    _recover_stale_sent_commands(command_types={WB_FETCH_ORDER_STICKER})
    now = timezone.now()
    command_ids = list(
        FbsMarketplaceCommand.objects.filter(
            status__in=ACTIVE_COMMAND_STATUSES,
            command_type=WB_FETCH_ORDER_STICKER,
            profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            profile__is_active=True,
            profile__outbox_enabled=True,
        )
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("created_at", "id")
        .values_list("id", flat=True)[: max(int(limit), 1)]
    )
    results = [
        process_marketplace_command(command_id=command_id, transport=transport)
        for command_id in command_ids
    ]
    return MarketplaceQueueResult(
        inspected=len(results),
        confirmed=sum(
            command.status == FbsMarketplaceCommand.STATUS_CONFIRMED
            for command in results
        ),
        retry=sum(
            command.status == FbsMarketplaceCommand.STATUS_RETRY
            for command in results
        ),
        failed=sum(
            command.status == FbsMarketplaceCommand.STATUS_FAILED
            for command in results
        ),
        conflict=sum(
            command.status == FbsMarketplaceCommand.STATUS_CONFLICT
            for command in results
        ),
    )


def _run_marketplace_queue_schedulers_once(limit: int) -> None:
    _schedule_preloaded_ozon_order_barcodes(limit)
    _recover_ozon_semantic_ship_failures(limit)
    _run_marketplace_queue_interactive_schedulers_once(limit)
    _schedule_pending_handover_commands(limit)
    _schedule_open_wb_handover_readbacks(limit)
    _recover_stale_sent_commands()


def _run_marketplace_queue_interactive_schedulers_once(limit: int) -> None:
    """Schedule controller-facing work on every fast worker polling pass."""
    _schedule_active_wb_verification_label_requests(limit)
    _schedule_verified_handover_assignments(limit)
    _schedule_prepared_metadata_commands(limit)
    _schedule_waiting_label_commands(limit)


def _schedule_preloaded_ozon_order_barcodes(limit: int) -> None:
    """Preload the stable Ozon QR as soon as each order is picked.

    This does not call Ozon ``ship`` or claim that the posting is packed.  It
    only persists the barcode already received with the posting, while the
    picker continues the active wave.  The ordinary controller path remains
    a fallback for transient failures.
    """
    prepared_label = FbsOrderLabel.objects.filter(order_id=OuterRef("pk")).filter(
        Q(status=FbsOrderLabel.STATUS_APPLIED)
        | Q(
            status__in=(
                FbsOrderLabel.STATUS_REQUESTED,
                FbsOrderLabel.STATUS_READY,
            ),
            barcode__gt="",
        )
    )
    order_ids = list(
        FbsOrder.objects.filter(
            profile__marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            internal_status=FbsOrder.STATUS_PICKED,
            pick_tasks__status=FbsPickTask.STATUS_PICKED,
            pick_tasks__batch__status__in=(
                FbsPickBatch.STATUS_IN_PROGRESS,
                FbsPickBatch.STATUS_VERIFICATION,
            ),
        )
        .annotate(_has_preloaded_order_qr=Exists(prepared_label))
        .filter(_has_preloaded_order_qr=False)
        .order_by("pick_tasks__batch_id", "pick_tasks__sort_order", "id")
        .values_list("id", flat=True)
        .distinct()[: max(int(limit), 1)]
    )
    for order_id in order_ids:
        try:
            prefetch_ozon_order_label_barcode(order_id=order_id)
        except FbsError:
            logger.exception(
                "FBS Ozon preloaded order QR failed for active order %s",
                order_id,
            )


def _run_marketplace_queue_interactive_schedulers(limit: int) -> None:
    """Replay only a fast scheduler pass that PostgreSQL fully rolled back."""
    for attempt in range(MARKETPLACE_DEADLOCK_RETRY_ATTEMPTS):
        try:
            _run_marketplace_queue_interactive_schedulers_once(limit)
            return
        except OperationalError as exc:
            if (
                not _is_database_deadlock(exc)
                or attempt + 1 >= MARKETPLACE_DEADLOCK_RETRY_ATTEMPTS
            ):
                raise
            logger.warning(
                "FBS interactive marketplace scheduler deadlock; retrying pass attempt=%s",
                attempt + 2,
            )
            time.sleep(MARKETPLACE_DEADLOCK_RETRY_DELAY_SECONDS * (attempt + 1))


def _run_marketplace_queue_schedulers(limit: int) -> None:
    """Replay only a scheduler pass that PostgreSQL fully rolled back."""
    for attempt in range(MARKETPLACE_DEADLOCK_RETRY_ATTEMPTS):
        try:
            _run_marketplace_queue_schedulers_once(limit)
            return
        except OperationalError as exc:
            if (
                not _is_database_deadlock(exc)
                or attempt + 1 >= MARKETPLACE_DEADLOCK_RETRY_ATTEMPTS
            ):
                raise
            logger.warning(
                "FBS marketplace scheduler deadlock; retrying pass attempt=%s",
                attempt + 2,
            )
            time.sleep(MARKETPLACE_DEADLOCK_RETRY_DELAY_SECONDS * (attempt + 1))


def _prioritize_interactive_label_commands(command_queryset):
    requested_label_for_order = FbsOrderLabel.objects.filter(
        order_id=OuterRef("order_id"),
        status=FbsOrderLabel.STATUS_REQUESTED,
    )
    priority_cases = [
        When(has_requested_label=True, then=Value(0)),
        *[
            When(command_type__in=command_types, then=Value(priority))
            for command_types, priority in MARKETPLACE_QUEUE_PRIORITY_GROUPS
        ],
    ]
    return command_queryset.annotate(
        has_requested_label=Exists(requested_label_for_order),
        interactive_priority=Case(
            *priority_cases,
            default=Value(MARKETPLACE_QUEUE_BACKGROUND_PRIORITY),
            output_field=IntegerField(),
        ),
    )


def _marketplace_queue_priority_value(
    command_type: str,
    *,
    has_requested_label: bool = False,
) -> int:
    if has_requested_label:
        return 0
    for command_types, priority in MARKETPLACE_QUEUE_PRIORITY_GROUPS:
        if command_type in command_types:
            return priority
    return MARKETPLACE_QUEUE_BACKGROUND_PRIORITY


def process_marketplace_queue(
    *,
    limit: int = 50,
    transport: MarketplaceTransport | None = None,
    run_schedulers: bool = True,
    run_interactive_schedulers: bool = False,
) -> MarketplaceQueueResult:
    _require_outbox()
    if run_schedulers:
        _run_marketplace_queue_schedulers(limit)
    elif run_interactive_schedulers:
        _run_marketplace_queue_interactive_schedulers(limit)
    now = timezone.now()
    command_queryset = FbsMarketplaceCommand.objects.filter(
        status__in=ACTIVE_COMMAND_STATUSES,
        profile__is_active=True,
        profile__outbox_enabled=True,
    )
    if not feature_enabled("marking_push"):
        command_queryset = command_queryset.exclude(command_type__in=METADATA_COMMAND_TYPES)
    else:
        command_queryset = command_queryset.exclude(
            Q(command_type__in=METADATA_COMMAND_TYPES)
            & Q(profile__marking_push_enabled=False)
        )
    terminal_wb_order_filter = Q(command_type=WB_ADD_ORDER_TO_HANDOVER) & (
        Q(order__marketplace_status__in=WB_TERMINAL_ORDER_STATUSES)
        | Q(order__marketplace_substatus__in=WB_TERMINAL_ORDER_STATUSES)
    )
    command_queryset = _prioritize_interactive_label_commands(
        command_queryset.filter(
            Q(next_attempt_at__isnull=True)
            | Q(next_attempt_at__lte=now)
            | terminal_wb_order_filter
        )
    )
    results = []
    for _index in range(max(int(limit), 1)):
        # Do not snapshot a large FIFO batch here. A controller can request a
        # label while an older marketplace command is in flight; re-reading
        # the next row lets that interactive label run immediately after the
        # current API call instead of waiting behind the rest of the batch.
        command_id = (
            command_queryset
            .order_by("interactive_priority", "created_at", "id")
            .values_list("id", flat=True)
            .first()
        )
        if command_id is None:
            break
        results.append(
            process_marketplace_command(command_id=command_id, transport=transport)
        )
    return MarketplaceQueueResult(
        inspected=len(results),
        confirmed=sum(command.status == FbsMarketplaceCommand.STATUS_CONFIRMED for command in results),
        retry=sum(command.status == FbsMarketplaceCommand.STATUS_RETRY for command in results),
        failed=sum(command.status == FbsMarketplaceCommand.STATUS_FAILED for command in results),
        conflict=sum(command.status == FbsMarketplaceCommand.STATUS_CONFLICT for command in results),
    )
