from __future__ import annotations

import hashlib
from pathlib import Path

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from employees.models import Employee
from fbs.exceptions import FbsFeatureDisabled, FbsLabelError
from fbs.flags import feature_enabled
from fbs.models import (
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsPickBatch,
    FbsPickException,
    FbsPickScanEvent,
    FbsPickTask,
)

from .scanning import record_order_label_scan_events


def _require_module() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")


def _require_label_confirmation() -> None:
    _require_module()
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Подтверждение этикеток FBS выключено.")


def _authenticated_user(user):
    return user if getattr(user, "is_authenticated", False) else None


def is_preloaded_ozon_order_label(label: FbsOrderLabel) -> bool:
    payload = label.payload if isinstance(label.payload, dict) else {}
    return bool(
        label.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
        and label.status == FbsOrderLabel.STATUS_REQUESTED
        and str(label.barcode or "").strip()
        and payload.get("_preloaded_ozon_order_barcode") is True
    )


def is_preconfirmed_ozon_order_label(label: FbsOrderLabel) -> bool:
    payload = label.payload if isinstance(label.payload, dict) else {}
    return bool(
        is_preloaded_ozon_order_label(label)
        and payload.get("_preconfirmed_order_barcode_at")
    )


@transaction.atomic
def confirm_preloaded_ozon_order_label_after_marketplace_advance(
    *,
    order_id: int,
    barcodes: dict[str, str],
    marketplace_status: str,
) -> FbsOrderLabel | None:
    """Confirm a physically scanned Ozon QR after the posting skipped past label fetch.

    Ozon stops serving the package-label PDF after some postings advance beyond
    ``awaiting_deliver``.  The recovery is safe only when the controller already
    scanned the preloaded stable QR and that exact QR is still present in the
    live posting response.
    """
    _require_module()
    label = (
        FbsOrderLabel.objects.select_for_update(of=("self",))
        .filter(order_id=order_id)
        .order_by("-requested_at", "-id")
        .first()
    )
    if label is None:
        return None

    live_barcodes = {
        str(value or "").strip()
        for value in (barcodes or {}).values()
        if str(value or "").strip()
    }
    local_barcode = str(label.barcode or "").strip()
    if not local_barcode or local_barcode not in live_barcodes:
        raise FbsLabelError(
            "Текущий ШК Ozon не совпал с QR, который контролер нанес на заказ."
        )
    if label.status in {
        FbsOrderLabel.STATUS_READY,
        FbsOrderLabel.STATUS_APPLIED,
    }:
        return label
    if not is_preconfirmed_ozon_order_label(label):
        return None

    now = timezone.now()
    payload = dict(label.payload) if isinstance(label.payload, dict) else {}
    payload.update(
        {
            "_ozon_marketplace_advanced_status": str(
                marketplace_status or ""
            ).strip().lower(),
            "_ozon_marketplace_advanced_at": now.isoformat(),
            "_ozon_live_barcode_confirmed": True,
        }
    )
    try:
        applied_by_id = (
            int(payload.get("_preconfirmed_order_barcode_by_id") or 0) or None
        )
    except (TypeError, ValueError):
        applied_by_id = None
    label.status = FbsOrderLabel.STATUS_APPLIED
    label.error = ""
    label.payload = payload
    label.ready_at = label.ready_at or now
    label.applied_at = label.applied_at or now
    label.applied_by_id = applied_by_id
    label.full_clean()
    label.save(
        update_fields=[
            "status",
            "error",
            "payload",
            "ready_at",
            "applied_at",
            "applied_by",
            "updated_at",
        ]
    )
    return label


def _assert_order_pick_verified(order_id: int) -> None:
    allocations = FbsOrderStockAllocation.objects.filter(
        order_item__order_id=order_id,
        pick_task__isnull=False,
    ).exclude(
        status__in=(
            FbsOrderStockAllocation.STATUS_RELEASED,
            FbsOrderStockAllocation.STATUS_CANCELED,
        )
    )
    if not allocations.exists():
        return
    if allocations.exclude(status=FbsOrderStockAllocation.STATUS_PICKED).exists():
        raise FbsLabelError("Отбор заказа еще не завершен полностью.")
    if allocations.filter(
        Q(verification_progress__isnull=True)
        | Q(verification_progress__qty_verified__lt=F("qty_picked"))
    ).exists():
        raise FbsLabelError("Повторная проверка товара еще не завершена.")


def _assert_order_has_no_open_shortage(order_id: int) -> None:
    open_shortages = FbsPickException.objects.filter(
        task__order_id=order_id,
        exception_type=FbsPickException.TYPE_NOT_FOUND,
        status=FbsPickException.STATUS_OPEN,
    )
    completed_repicks = FbsOrderStockAllocation.objects.filter(
        order_item_id=OuterRef("allocation__order_item_id"),
        status=FbsOrderStockAllocation.STATUS_PICKED,
        pick_task__status=FbsPickTask.STATUS_PICKED,
        pick_task__created_at__gt=OuterRef("created_at"),
        qty_picked__gte=OuterRef("allocation__qty_reserved"),
    )
    blocking_shortages = open_shortages.annotate(
        has_completed_repick=Exists(completed_repicks),
    ).exclude(
        Q(task__status=FbsPickTask.STATUS_CANCELED)
        & Q(
            allocation__status__in=(
                FbsOrderStockAllocation.STATUS_RELEASED,
                FbsOrderStockAllocation.STATUS_CANCELED,
            )
        )
        & Q(has_completed_repick=True)
    )
    if blocking_shortages.exists():
        raise FbsLabelError(
            "В заказе не хватает позиций. Отправьте его в проблемные — "
            "обычная передача запрещена."
        )


def _saved_file_hash(label: FbsOrderLabel) -> str:
    digest = hashlib.sha256()
    label.file.open("rb")
    try:
        for chunk in label.file.chunks():
            digest.update(chunk)
    finally:
        label.file.close()
    return digest.hexdigest()


def _require_label_actor(user):
    actor = _authenticated_user(user)
    if actor is None:
        raise FbsLabelError("Не указан сотрудник, подтвердивший этикетку.")
    allowed_roles = {
        "fbs_controller",
        "storekeeper",
        "head_manager",
        "director",
        "admin",
    }
    has_allowed_role = Employee.objects.filter(
        user=actor,
        is_active=True,
        role__in=allowed_roles,
    ).exists()
    if not has_allowed_role and not actor.is_superuser and actor.username != "dev":
        raise FbsLabelError("У сотрудника нет доступа к подтверждению этикеток FBS.")
    return actor


def _get_or_create_open_order_label(
    *, order: FbsOrder, requested_by=None
) -> FbsOrderLabel:
    applied = (
        FbsOrderLabel.objects.select_for_update()
        .filter(
            order=order,
            status=FbsOrderLabel.STATUS_APPLIED,
        )
        .order_by("-applied_at", "-id")
        .first()
    )
    if applied is not None:
        FbsOrderLabel.objects.filter(
            order=order,
            status__in=(
                FbsOrderLabel.STATUS_REQUESTED,
                FbsOrderLabel.STATUS_READY,
            ),
        ).update(
            status=FbsOrderLabel.STATUS_CANCELED,
            error="Этикетка заказа уже была подтверждена ранее.",
        )
        return applied
    existing = (
        FbsOrderLabel.objects.select_for_update()
        .filter(
            order=order,
            status__in=(FbsOrderLabel.STATUS_REQUESTED, FbsOrderLabel.STATUS_READY),
        )
        .order_by("id")
        .first()
    )
    if existing is not None:
        return existing
    return FbsOrderLabel.objects.create(
        order=order,
        marketplace=order.profile.marketplace,
        requested_by=_authenticated_user(requested_by),
    )


@transaction.atomic
def prefetch_wb_order_label_request(
    *, order_id: int, requested_by=None
) -> FbsOrderLabel:
    """Request a WB sticker while the controller is still checking the order."""
    _require_module()
    order = (
        FbsOrder.objects.select_for_update(of=("self",), nowait=True)
        .select_related("profile")
        .get(pk=order_id)
    )
    if order.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        raise FbsLabelError("Предварительная подготовка этикетки доступна только для WB.")
    if order.internal_status != FbsOrder.STATUS_PICKED:
        raise FbsLabelError("Этикетку можно запросить только после завершения отбора.")
    _assert_order_has_no_open_shortage(order.id)
    allocations = FbsOrderStockAllocation.objects.filter(
        order_item__order_id=order.id,
        pick_task__isnull=False,
    ).exclude(
        status__in=(
            FbsOrderStockAllocation.STATUS_RELEASED,
            FbsOrderStockAllocation.STATUS_CANCELED,
        )
    )
    if allocations.exclude(status=FbsOrderStockAllocation.STATUS_PICKED).exists():
        raise FbsLabelError("Отбор заказа еще не завершен полностью.")
    return _get_or_create_open_order_label(
        order=order,
        requested_by=requested_by,
    )


@transaction.atomic
def prefetch_ozon_order_label_barcode(
    *, order_id: int, requested_by=None
) -> FbsOrderLabel:
    """Store Ozon's stable order barcode before controller verification.

    This does not claim that the marketplace PDF is ready.  The label remains
    requested until Ozon accepts required metadata, assembles the posting and
    returns the official package-label document.
    """
    _require_module()
    order = (
        FbsOrder.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=order_id)
    )
    if order.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_OZON:
        raise FbsLabelError("Предварительный ШК заказа доступен только для Ozon.")
    if order.internal_status != FbsOrder.STATUS_PICKED:
        raise FbsLabelError("ШК заказа можно подготовить только после завершения отбора.")
    _assert_order_has_no_open_shortage(order.id)
    allocations = FbsOrderStockAllocation.objects.filter(
        order_item__order_id=order.id,
        pick_task__isnull=False,
    ).exclude(
        status__in=(
            FbsOrderStockAllocation.STATUS_RELEASED,
            FbsOrderStockAllocation.STATUS_CANCELED,
        )
    )
    if allocations.exclude(status=FbsOrderStockAllocation.STATUS_PICKED).exists():
        raise FbsLabelError("Отбор заказа еще не завершен полностью.")

    from fbs.integrations.ozon import _ozon_order_label_barcodes

    barcodes = _ozon_order_label_barcodes(order)
    if not barcodes:
        raise FbsLabelError("Ozon не передал ШК заказа при загрузке отправления.")
    label = _get_or_create_open_order_label(
        order=order,
        requested_by=requested_by,
    )
    if label.status == FbsOrderLabel.STATUS_APPLIED:
        return label
    barcode = barcodes[0]
    existing_barcode = str(label.barcode or "").strip()
    if existing_barcode and existing_barcode != barcode:
        raise FbsLabelError("Ozon изменил заранее загруженный ШК заказа.")
    payload = dict(label.payload) if isinstance(label.payload, dict) else {}
    payload.update(
        {
            "_preloaded_ozon_order_barcode": True,
            "_preloaded_ozon_order_barcodes": list(barcodes),
        }
    )
    label.external_label_id = label.external_label_id or order.external_order_id
    label.barcode = barcode
    label.payload = payload
    label.save(
        update_fields=[
            "external_label_id",
            "barcode",
            "payload",
            "updated_at",
        ]
    )
    return label


@transaction.atomic
def ensure_order_label_request(*, order_id: int, requested_by=None) -> FbsOrderLabel:
    _require_module()
    order = (
        FbsOrder.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=order_id)
    )
    if order.internal_status != FbsOrder.STATUS_PICKED:
        raise FbsLabelError("Этикетку можно запросить только после завершения отбора.")
    _assert_order_has_no_open_shortage(order.id)
    _assert_order_pick_verified(order.id)
    return _get_or_create_open_order_label(
        order=order,
        requested_by=requested_by,
    )


@transaction.atomic
def register_marketplace_label(
    *,
    order_id: int,
    external_label_id: str,
    barcode: str,
    label_format: str = FbsOrderLabel.FORMAT_PDF,
    file_url: str = "",
    payload: dict | None = None,
    content_hash: str = "",
    file_name: str = "",
    file_content: bytes | None = None,
) -> FbsOrderLabel:
    _require_module()
    external_label_id = str(external_label_id or "").strip()
    barcode = str(barcode or "").strip()
    label_format = str(label_format or "").strip().lower()
    file_content = bytes(file_content) if file_content is not None else None
    if not external_label_id:
        raise FbsLabelError("Маркетплейс не передал идентификатор этикетки.")
    if not barcode:
        raise FbsLabelError("Маркетплейс не передал штрихкод этикетки.")
    if label_format not in dict(FbsOrderLabel.FORMAT_CHOICES):
        raise FbsLabelError("Маркетплейс передал неподдерживаемый формат этикетки.")
    if file_content is not None:
        if not file_content or len(file_content) > 20 * 1024 * 1024:
            raise FbsLabelError("Размер файла этикетки недопустим.")
        file_name = Path(str(file_name or "").strip()).name
        if not file_name:
            raise FbsLabelError("Не указано имя файла этикетки.")
        calculated_hash = hashlib.sha256(file_content).hexdigest()
        if content_hash and content_hash != calculated_hash:
            raise FbsLabelError("Контрольная сумма файла этикетки не совпадает.")
        content_hash = calculated_hash

    order = (
        FbsOrder.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=order_id)
    )
    if order.internal_status != FbsOrder.STATUS_PICKED:
        raise FbsLabelError("Этикетку нельзя зарегистрировать до завершения отбора.")
    _assert_order_has_no_open_shortage(order.id)
    # WB-этикетка предзагружается в начале контроля, чтобы принтер не ждал
    # маркетплейс после проверки товара. Сохранить уже полученный файл можно
    # заранее; применить его к заказу confirm_order_label_scan по-прежнему не
    # позволит до полной повторной проверки.
    if order.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        _assert_order_pick_verified(order.id)
    label = (
        FbsOrderLabel.objects.select_for_update(of=("self",))
        .filter(
            order=order,
            status__in=(FbsOrderLabel.STATUS_REQUESTED, FbsOrderLabel.STATUS_READY),
        )
        .order_by("id")
        .first()
    )
    if label is None:
        label = FbsOrderLabel(
            order=order,
            marketplace=order.profile.marketplace,
        )
    elif label.status == FbsOrderLabel.STATUS_READY:
        if label.external_label_id != external_label_id or label.barcode != barcode:
            raise FbsLabelError("Для заказа уже зарегистрирована другая активная этикетка.")

    existing_payload = dict(label.payload) if isinstance(label.payload, dict) else {}
    if (
        label.status == FbsOrderLabel.STATUS_REQUESTED
        and existing_payload.get("_preloaded_ozon_order_barcode") is True
        and str(label.barcode or "").strip()
        and str(label.barcode or "").strip() != barcode
    ):
        raise FbsLabelError(
            "ШК официальной этикетки Ozon не совпал с заранее загруженным ШК заказа."
        )

    label.external_label_id = external_label_id
    label.barcode = barcode
    label.label_format = label_format
    label.file_url = str(file_url or "").strip()
    label.payload = payload if isinstance(payload, dict) else {}
    for key, value in existing_payload.items():
        if str(key).startswith(("_preloaded_", "_preconfirmed_")):
            label.payload[key] = value
    incoming_hash = str(content_hash or "").strip()
    if file_content is not None and label.file:
        saved_hash = str(label.content_hash or "").strip() or _saved_file_hash(label)
        if saved_hash != calculated_hash:
            raise FbsLabelError("Маркетплейс повторно передал другой файл этикетки.")
        incoming_hash = saved_hash
    if incoming_hash:
        label.content_hash = incoming_hash
    preconfirmed_order_barcode = bool(
        order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
        and existing_payload.get("_preloaded_ozon_order_barcode") is True
        and existing_payload.get("_preconfirmed_order_barcode_at")
    )
    label.status = (
        FbsOrderLabel.STATUS_APPLIED
        if preconfirmed_order_barcode
        else FbsOrderLabel.STATUS_READY
    )
    label.error = ""
    label.ready_at = label.ready_at or timezone.now()
    if preconfirmed_order_barcode:
        try:
            label.applied_by_id = int(
                existing_payload.get("_preconfirmed_order_barcode_by_id") or 0
            ) or None
        except (TypeError, ValueError):
            label.applied_by_id = None
        label.applied_at = label.applied_at or timezone.now()
    if file_content is not None and not label.file:
        label.file.save(file_name, ContentFile(file_content), save=False)
        label.file_url = ""
    label.full_clean()
    label.save()
    if preconfirmed_order_barcode:
        order.internal_status = FbsOrder.STATUS_READY_FOR_HANDOVER
        order.save(update_fields=["internal_status", "updated_at"])
    label_id = label.id

    def finalize_registered_label() -> None:
        from .picking import refresh_pick_batch_verification
        from .totes import refresh_check_tote_status

        # The controller already printed and scanned this exact Ozon QR from
        # the import payload.  Keep the official PDF for audit, but never send
        # a second physical label to the printer.
        if not preconfirmed_order_barcode:
            from .printing import queue_fbs_order_label_print

            queue_fbs_order_label_print(label_id=label_id)
        check_tote_ids = (
            FbsOrderLabel.objects.filter(pk=label_id)
            .values_list("controller_tote_orders__check_tote_id", flat=True)
            .exclude(controller_tote_orders__check_tote_id__isnull=True)
            .distinct()
        )
        for check_tote_id in check_tote_ids:
            refresh_check_tote_status(check_tote_id=check_tote_id)
        if preconfirmed_order_barcode:
            batch_ids = FbsPickBatch.objects.filter(
                tasks__order_id=order_id,
            ).values_list("id", flat=True).distinct()
            for batch_id in batch_ids:
                refresh_pick_batch_verification(batch_id=batch_id)

    transaction.on_commit(finalize_registered_label, robust=True)
    return label


@transaction.atomic
def confirm_order_label_scan(
    *,
    label_id: int,
    label_scan: str,
    performed_by,
) -> FbsOrderLabel:
    _require_label_confirmation()
    actor = _require_label_actor(performed_by)
    label_scan = str(label_scan or "").strip()
    label = (
        FbsOrderLabel.objects.select_for_update(of=("self",))
        .select_related("order__profile")
        .get(pk=label_id)
    )
    order = FbsOrder.objects.select_for_update().get(pk=label.order_id)
    label.order = order
    active_verification_id = (
        FbsPickBatch.objects.filter(
            tasks__order=order,
            status=FbsPickBatch.STATUS_VERIFICATION,
        )
        .order_by("id")
        .values_list("id", flat=True)
        .first()
    )
    active_verification = (
        FbsPickBatch.objects.select_for_update()
        .filter(pk=active_verification_id)
        .first()
        if active_verification_id is not None
        else None
    )
    if (
        active_verification is not None
        and active_verification.verification_assigned_to_id != actor.id
    ):
        raise FbsLabelError("Этикетку должен подтвердить оператор назначенной проверки.")
    reused_applied_label = label.status == FbsOrderLabel.STATUS_APPLIED
    preloaded_ozon_label = is_preloaded_ozon_order_label(label)
    if reused_applied_label:
        if label_scan != label.barcode:
            raise FbsLabelError("Скан не совпадает с этикеткой этого заказа.")
        if order.internal_status == FbsOrder.STATUS_READY_FOR_HANDOVER:
            record_order_label_scan_events(
                order_id=order.id,
                scan_value=label_scan,
                expected_value=label.barcode,
                result=FbsPickScanEvent.RESULT_SUCCESS,
                message="Этикетка заказа уже была подтверждена.",
                created_by=actor,
            )
            return label
        if (
            active_verification is None
            or order.internal_status != FbsOrder.STATUS_PICKED
        ):
            raise FbsLabelError("Этикетка уже подтверждена.")
    elif not preloaded_ozon_label and label.status != FbsOrderLabel.STATUS_READY:
        raise FbsLabelError("Этикетка маркетплейса еще не готова.")
    if order.internal_status != FbsOrder.STATUS_PICKED:
        raise FbsLabelError("Заказ не находится в статусе завершенного отбора.")
    _assert_order_has_no_open_shortage(order.id)
    _assert_order_pick_verified(order.id)
    if label.marketplace != order.profile.marketplace:
        raise FbsLabelError("Этикетка относится к другому маркетплейсу.")
    if label_scan != label.barcode:
        raise FbsLabelError("Скан не совпадает с этикеткой этого заказа.")

    if preloaded_ozon_label:
        payload = dict(label.payload) if isinstance(label.payload, dict) else {}
        payload["_preconfirmed_order_barcode_at"] = timezone.now().isoformat()
        payload["_preconfirmed_order_barcode_by_id"] = actor.id
        label.payload = payload
        label.error = ""
        label.save(update_fields=["payload", "error", "updated_at"])
        record_order_label_scan_events(
            order_id=order.id,
            scan_value=label_scan,
            expected_value=label.barcode,
            result=FbsPickScanEvent.RESULT_SUCCESS,
            message=(
                "ШК заказа закреплен за проверенным товаром; "
                "подтверждение Ozon выполняется в фоне."
            ),
            created_by=actor,
        )
        # Queue Ozon packaging immediately after the controller's QR scan.
        # This only writes an outbox command; network validation and official
        # label retrieval continue in the background.
        from .marketplace import schedule_label_preparation

        schedule_label_preparation(order_id=order.id)
        return label

    if order.profile.marketplace == "wb":
        # Финальная привязка WB-КИЗ выполняется только после того, как
        # контролер подтвердил ШК заказа. До этого в заказе хранится лишь
        # проверенный скан, а склад резервирует количество без КИЗ-связи.
        from .marketplace import schedule_marketplace_metadata
        from .picking import finalize_wb_order_markings
        from .traceability import prepare_order_marketplace_metadata

        finalize_wb_order_markings(
            order_id=order.id,
            performed_by=actor,
        )
        active_pick_task_id = (
            order.pick_tasks.filter(batch_id=getattr(active_verification, "id", None))
            .order_by("id")
            .values_list("id", flat=True)
            .first()
            if active_verification is not None
            else None
        )
        prepare_order_marketplace_metadata(
            order_id=order.id,
            pick_task_id=active_pick_task_id,
        )
        schedule_marketplace_metadata(order_id=order.id)

    if not reused_applied_label:
        now = timezone.now()
        label.status = FbsOrderLabel.STATUS_APPLIED
        label.applied_by = actor
        label.applied_at = now
        label.save(update_fields=["status", "applied_by", "applied_at", "updated_at"])
    order.internal_status = FbsOrder.STATUS_READY_FOR_HANDOVER
    order.save(update_fields=["internal_status", "updated_at"])
    record_order_label_scan_events(
        order_id=order.id,
        scan_value=label_scan,
        expected_value=label.barcode,
        result=FbsPickScanEvent.RESULT_SUCCESS,
        message=(
            "Этикетка заказа повторно подтверждена в текущей проверке."
            if reused_applied_label
            else "Этикетка заказа подтверждена."
        ),
        created_by=actor,
    )
    from .picking import refresh_pick_batch_verification

    for batch_id in order.pick_tasks.values_list("batch_id", flat=True):
        refresh_pick_batch_verification(batch_id=batch_id)
    return label
