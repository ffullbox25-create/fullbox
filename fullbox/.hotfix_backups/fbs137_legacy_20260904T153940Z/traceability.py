from __future__ import annotations

import hashlib
from dataclasses import dataclass

from django.db import transaction

from fbs.exceptions import FbsPickingError
from fbs.models import (
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderItem,
    FbsPickScanEvent,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
)


_MARKING_TOKENS = ("sgtin", "kiz", "marking", "mark_code", "markcode", "uin")
_EXPIRY_TOKENS = (
    "expiration",
    "expiry",
    "expire",
    "best_before",
    "bestbefore",
    "shelf_life",
    "shelflife",
)

OZON_MARKING_NOT_REQUIRED_EXTERNAL_STATUS = "ozon_mandatory_mark_not_required"
OZON_MARKING_REQUIRED = "required"
OZON_MARKING_NOT_REQUIRED = "not_required"
OZON_MARKING_UNKNOWN = "unknown"
LEGACY_MARKING_ABSENCE_EXPECTED_VALUE = "legacy_party_before_2025_10_01"


@dataclass(frozen=True)
class FbsMetadataRequirements:
    marking_required: bool
    expiry_required: bool
    marketplace_marking_required: bool


def _flatten_tokens(value) -> tuple[str, ...]:
    tokens: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            tokens.extend(_flatten_tokens(key))
            tokens.extend(_flatten_tokens(nested))
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            tokens.extend(_flatten_tokens(nested))
    elif value not in (None, "", False):
        tokens.append(str(value).strip().casefold().replace("-", "_"))
    return tuple(token for token in tokens if token)


def _contains_any(tokens: tuple[str, ...], needles: tuple[str, ...]) -> bool:
    return any(needle in token for token in tokens for needle in needles)


def _marking_signal_is_true(value) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "no", "none"}
    return bool(value)


def _ozon_item_identifiers(item: FbsOrderItem) -> set[str]:
    raw_payload_value = getattr(item, "raw_payload", None)
    raw_payload = raw_payload_value if isinstance(raw_payload_value, dict) else {}
    values = (
        getattr(item, "external_line_id", ""),
        getattr(item, "external_sku", ""),
        getattr(item, "barcode", ""),
        raw_payload.get("product_id"),
        raw_payload.get("sku"),
        raw_payload.get("offer_id"),
        raw_payload.get("offerId"),
        raw_payload.get("product_offer_id"),
    )
    return {str(value).strip() for value in values if str(value or "").strip()}


def ozon_marking_requirement_decision(item: FbsOrderItem) -> str:
    """Return Ozon's explicit three-state marking decision for one item."""
    order = getattr(item, "order", None)
    profile = getattr(order, "profile", None)
    marketplace = str(getattr(profile, "marketplace", "") or "").strip().casefold()
    if marketplace != "ozon":
        return OZON_MARKING_UNKNOWN

    requirements = item.requirements if isinstance(item.requirements, dict) else {}
    required_tokens = _flatten_tokens(requirements.get("required_meta"))
    raw_payload_value = getattr(item, "raw_payload", None)
    raw_payload = raw_payload_value if isinstance(raw_payload_value, dict) else {}
    positive_product_keys = (
        "is_kiz",
        "mandatory_mark",
        "is_mandatory_mark_needed",
        "mandatory_mark_needed",
    )
    if (
        _marking_signal_is_true(requirements.get("is_kiz"))
        or _marking_signal_is_true(requirements.get("mandatory_mark"))
        or _contains_any(required_tokens, _MARKING_TOKENS)
        or any(
            key in raw_payload and _marking_signal_is_true(raw_payload.get(key))
            for key in positive_product_keys
        )
    ):
        return OZON_MARKING_REQUIRED

    explicit_product_no = bool(
        any(
            key in raw_payload and not _marking_signal_is_true(raw_payload.get(key))
            for key in ("is_kiz", "mandatory_mark", "mandatory_mark_needed")
        )
        or (
            raw_payload.get("is_mandatory_mark_needed") is False
            and raw_payload.get("is_mandatory_mark_possible") is False
        )
    )
    order_raw_payload = (
        order.raw_payload
        if isinstance(getattr(order, "raw_payload", None), dict)
        else {}
    )
    order_requirements = order_raw_payload.get("requirements")
    required_product_values = (
        order_requirements.get("products_requiring_mandatory_mark")
        if isinstance(order_requirements, dict)
        else None
    )
    if isinstance(required_product_values, (list, tuple, set)):
        required_product_ids = {
            str(value).strip()
            for value in required_product_values
            if not isinstance(value, (dict, list, tuple, set))
            and str(value or "").strip()
        }
        if required_product_ids & _ozon_item_identifiers(item):
            return OZON_MARKING_REQUIRED
        return OZON_MARKING_NOT_REQUIRED
    if explicit_product_no:
        return OZON_MARKING_NOT_REQUIRED
    return OZON_MARKING_UNKNOWN


def marketplace_marking_transfer_allowed(item: FbsOrderItem) -> bool:
    """Return whether a scanned KIZ may be sent to this marketplace order."""
    return ozon_marking_requirement_decision(item) != OZON_MARKING_NOT_REQUIRED


def metadata_requirements(item: FbsOrderItem) -> FbsMetadataRequirements:
    requirements = item.requirements if isinstance(item.requirements, dict) else {}
    required_tokens = _flatten_tokens(requirements.get("required_meta"))
    order = getattr(item, "order", None)
    profile = getattr(order, "profile", None)
    marketplace = str(getattr(profile, "marketplace", "") or "").strip().casefold()
    wb_meta = requirements.get("wb_meta")
    wb_sgtin = wb_meta.get("sgtin") if isinstance(wb_meta, dict) else None
    wb_decision = (
        str(wb_sgtin.get("decision") or "").strip().casefold()
        if isinstance(wb_sgtin, dict)
        else ""
    )
    # `optionalMeta: sgtin` only means WB permits this metadata.  It is not a
    # legal marking decision.  A concrete WB value or any non-optional
    # validation decision is order-specific evidence that the KIZ must be
    # checked. For WB and legacy records, SKU flags and the reserved party's
    # marking_code remain independent product/party sources of truth.
    wb_sgtin_requires_marking = bool(
        marketplace == "wb"
        and isinstance(wb_sgtin, dict)
        and wb_sgtin.get("available")
        and (
            wb_sgtin.get("has_value")
            or wb_decision not in {"", "optional"}
        )
    )
    marketplace_requires_marking = bool(
        requirements.get("is_kiz")
        or requirements.get("mandatory_mark")
        or _contains_any(required_tokens, _MARKING_TOKENS)
        or wb_sgtin_requires_marking
    )
    ozon_decision = ozon_marking_requirement_decision(item)
    # Marketplace obligation and Fullbox's internal traceability obligation are
    # intentionally separate.  An explicit Ozon "not required" response only
    # disables external transfer; it must not erase the SKU-card requirement.
    if marketplace == "ozon" and ozon_decision == OZON_MARKING_REQUIRED:
        marketplace_marking_required = True
    elif marketplace == "ozon" and ozon_decision == OZON_MARKING_NOT_REQUIRED:
        marketplace_marking_required = False
    else:
        marketplace_marking_required = marketplace_requires_marking
    marking_required = bool(
        marketplace_marking_required
        or getattr(item.sku, "honest_sign", False)
    )
    expiry_required = _contains_any(required_tokens, _EXPIRY_TOKENS)
    return FbsMetadataRequirements(
        marking_required=marking_required,
        expiry_required=expiry_required,
        marketplace_marking_required=marketplace_marking_required,
    )


def controller_marking_scan_required(
    allocation: FbsOrderStockAllocation,
) -> bool:
    """Return whether the controller must scan a KIZ for this exact order."""
    requirements = metadata_requirements(allocation.order_item)
    return bool(
        requirements.marking_required
        or str(allocation.balance.marking_code or "").strip()
    )


def controller_legacy_marking_exception_allowed(
    allocation: FbsOrderStockAllocation,
) -> bool:
    """Allow a no-code legacy acknowledgement only for optional marketplace KIZ."""
    requirements = metadata_requirements(allocation.order_item)
    return bool(
        controller_marking_scan_required(allocation)
        and not requirements.marketplace_marking_required
        and not str(allocation.balance.marking_code or "").strip()
    )


def legacy_marking_absence_confirmed(
    allocation: FbsOrderStockAllocation,
    *,
    quantity_after: int | None = None,
) -> bool:
    """Return whether a controller audited this unit as an unmarked legacy party."""
    events = FbsPickScanEvent.objects.filter(
        allocation=allocation,
        stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
        result=FbsPickScanEvent.RESULT_SUCCESS,
        scan_value="",
        expected_value=LEGACY_MARKING_ABSENCE_EXPECTED_VALUE,
    )
    if quantity_after is not None:
        events = events.filter(quantity_after=quantity_after)
    return events.exists()


def ozon_marking_transfer_not_required(
    transfer: FbsMarketplaceMetadataTransfer | None,
) -> bool:
    """Return whether Ozon explicitly said this posting needs no KIZ."""
    if transfer is None:
        return False
    order = getattr(getattr(transfer, "order_item", None), "order", None)
    profile = getattr(order, "profile", None)
    marketplace = str(getattr(profile, "marketplace", "") or "").strip().casefold()
    return bool(
        marketplace == "ozon"
        and transfer.metadata_type
        == FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE
        and transfer.status == FbsMarketplaceMetadataTransfer.STATUS_CANCELED
        and not transfer.is_required
        and str(transfer.external_status or "").strip()
        == OZON_MARKING_NOT_REQUIRED_EXTERNAL_STATUS
    )


def marketplace_metadata_transfer_resolved(
    transfer: FbsMarketplaceMetadataTransfer | None,
) -> bool:
    """Return whether marketplace confirmation no longer blocks handover."""
    return bool(
        transfer is not None
        and (
            transfer.status == FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED
            or ozon_marking_transfer_not_required(transfer)
        )
    )


def create_allocation_trace(allocation: FbsOrderStockAllocation) -> FbsOrderTraceability:
    balance = allocation.balance
    # Для WB и Ozon конкретный КИЗ на этапе резерва является только
    # техническим носителем количества. Фактическая связь КИЗ -> заказ
    # создается по физическому скану контролера перед подготовкой метаданных.
    marketplace = str(allocation.order_item.order.profile.marketplace or "").strip()
    marking_code = (
        ""
        if marketplace in {"wb", "ozon"}
        else str(balance.marking_code or "").strip()
    )
    trace = FbsOrderTraceability(
        allocation=allocation,
        marking_code=marking_code,
        lot_code=str(balance.lot_code or "").strip(),
        expiry_date=balance.expiry_date,
        qty=allocation.qty_reserved,
    )
    trace.full_clean()
    trace.save()
    return trace


def set_allocation_trace_status(
    allocation: FbsOrderStockAllocation,
    status: str,
) -> FbsOrderTraceability:
    trace = FbsOrderTraceability.objects.select_for_update().get(allocation=allocation)
    if trace.status != status:
        trace.status = status
        trace.save(update_fields=["status", "updated_at"])
    return trace


def _idempotency_key(
    *,
    trace: FbsOrderTraceability,
    metadata_type: str,
    value: str,
) -> str:
    raw = f"{trace.allocation.order_item_id}:{trace.id}:{metadata_type}:{value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@transaction.atomic
def prepare_order_marketplace_metadata(
    *,
    order_id: int,
    pick_task_id: int | None = None,
) -> tuple[FbsMarketplaceMetadataTransfer, ...]:
    """Подготовить метаданные маркетплейса по отобранным резервам заказа.

    ``pick_task_id`` ограничивает выборку резервами текущего задания отбора.
    Без него в выборку попадают и резервы прошлых волн: у повторно собранного
    заказа рядом с новым ``picked`` остаётся старый ``released``, и заказ
    выглядит недособранным, хотя товар в таре.
    """
    order = FbsOrder.objects.select_for_update().get(pk=order_id)
    if order.internal_status != FbsOrder.STATUS_PICKED:
        raise FbsPickingError("Метаданные маркетплейса готовятся только после отбора заказа.")
    allocation_qs = FbsOrderStockAllocation.objects.select_for_update(
        of=("self",)
    ).select_related("order_item__sku", "traceability").filter(order_item__order=order)
    if pick_task_id is not None:
        allocation_qs = allocation_qs.filter(pick_task_id=pick_task_id)
    allocations = list(allocation_qs.order_by("order_item_id", "id"))
    if not allocations or any(
        allocation.status != FbsOrderStockAllocation.STATUS_PICKED
        for allocation in allocations
    ):
        raise FbsPickingError("Не все позиции заказа отобраны для подготовки метаданных.")

    transfers: list[FbsMarketplaceMetadataTransfer] = []
    for allocation in allocations:
        try:
            trace = allocation.traceability
        except FbsOrderTraceability.DoesNotExist as exc:
            raise FbsPickingError("У отобранной позиции отсутствует трассировка партии.") from exc
        requirements = metadata_requirements(allocation.order_item)
        legacy_absence = legacy_marking_absence_confirmed(
            allocation,
            quantity_after=int(allocation.qty_picked or 0),
        )
        if (
            requirements.marking_required
            and not trace.marking_code
            and not legacy_absence
        ):
            raise FbsPickingError(
                f"Строка {allocation.order_item.external_line_id}: отсутствует обязательный КИЗ."
            )
        if legacy_absence and requirements.marketplace_marking_required:
            raise FbsPickingError(
                f"Строка {allocation.order_item.external_line_id}: площадка требует КИЗ; "
                "пропуск для старой партии запрещен."
            )
        if requirements.expiry_required and trace.expiry_date is None:
            raise FbsPickingError(
                f"Строка {allocation.order_item.external_line_id}: отсутствует обязательный срок годности."
            )

        values = []
        if trace.marking_code and marketplace_marking_transfer_allowed(
            allocation.order_item
        ):
            values.append(
                (
                    FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
                    trace.marking_code,
                    requirements.marketplace_marking_required,
                )
            )
        if trace.expiry_date:
            values.append(
                (
                    FbsMarketplaceMetadataTransfer.TYPE_EXPIRATION,
                    trace.expiry_date.isoformat(),
                    requirements.expiry_required,
                )
            )
        for metadata_type, value, is_required in values:
            transfer, created = FbsMarketplaceMetadataTransfer.objects.get_or_create(
                traceability=trace,
                metadata_type=metadata_type,
                defaults={
                    "order_item": allocation.order_item,
                    "value": value,
                    "is_required": is_required,
                    "idempotency_key": _idempotency_key(
                        trace=trace,
                        metadata_type=metadata_type,
                        value=value,
                    ),
                },
            )
            if not created and (
                transfer.value != value or transfer.is_required != is_required
            ):
                if transfer.status != FbsMarketplaceMetadataTransfer.STATUS_PREPARED:
                    raise FbsPickingError("Подготовленные метаданные уже передавались и изменились.")
                transfer.value = value
                transfer.is_required = is_required
                transfer.idempotency_key = _idempotency_key(
                    trace=trace,
                    metadata_type=metadata_type,
                    value=value,
                )
                transfer.save(
                    update_fields=["value", "is_required", "idempotency_key", "updated_at"]
                )
            transfers.append(transfer)
    return tuple(transfers)
