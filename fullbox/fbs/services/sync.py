from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta, timezone as datetime_timezone

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from billing.permissions import filter_agencies_for_user
from employees.models import Employee
from fbs.exceptions import FbsFeatureDisabled, FbsIntegrationError
from fbs.barcode_aliases import _normalize_size
from fbs.flags import feature_enabled
from fbs.integrations.contracts import NormalizedMarketplaceItem, NormalizedMarketplaceOrder
from fbs.integrations.http import (
    MarketplaceHttpResponse,
    MarketplaceReadTransport,
    RequestsMarketplaceReadTransport,
)
from fbs.integrations.ozon import (
    build_ozon_posting_status_spec,
    build_ozon_postings_spec,
    parse_ozon_posting_status,
    parse_ozon_postings,
)
from fbs.integrations.wb import (
    build_wb_new_orders_spec,
    build_wb_orders_metadata_spec,
    build_wb_statuses_spec,
    enrich_wb_orders_with_metadata,
    parse_wb_new_orders,
    parse_wb_orders_metadata,
    parse_wb_statuses,
)
from fbs.models import (
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsMarketplaceEvent,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsPickTask,
    FbsStockBalance,
    FbsStockExportState,
    FbsSyncCursor,
)
from sku.models import Agency, MarketplaceBinding, SKU, SKUBarcode
from sklad.models import WarehouseStockSnapshot


@dataclass(frozen=True)
class MarketplaceSyncResult:
    requests: int = 0
    received: int = 0
    created: int = 0
    updated: int = 0
    duplicate: int = 0
    skipped: int = 0


def _add_results(left: MarketplaceSyncResult, right: MarketplaceSyncResult) -> MarketplaceSyncResult:
    return MarketplaceSyncResult(
        requests=left.requests + right.requests,
        received=left.received + right.received,
        created=left.created + right.created,
        updated=left.updated + right.updated,
        duplicate=left.duplicate + right.duplicate,
        skipped=left.skipped + right.skipped,
    )


_MANUAL_ORDER_PULL_ROLES = (
    "manager",
    "head_manager",
    "director",
    "admin",
    "developer",
)


def _require_manual_order_pull_actor(actor, *, agency_id: int) -> None:
    user_model = get_user_model()
    if not isinstance(actor, user_model) or not actor.is_authenticated:
        raise FbsIntegrationError(
            "Ручное обновление FBS-заказов доступно только менеджерам и руководителям."
        )
    if actor.is_superuser or actor.username == "dev":
        return
    employee = Employee.objects.filter(
        user=actor,
        is_active=True,
    ).first()
    if employee is None or not any(
        employee.has_role(role) for role in _MANUAL_ORDER_PULL_ROLES
    ):
        raise FbsIntegrationError(
            "Ручное обновление FBS-заказов доступно только менеджерам и руководителям."
        )
    if not filter_agencies_for_user(
        Agency.objects.filter(pk=agency_id),
        actor,
    ).exists():
        raise FbsIntegrationError(
            "Нет доступа к ручному обновлению FBS-заказов этого клиента."
        )


def _profile_for_sync(
    profile_id: int,
    *,
    stream: str,
    allow_manual_order_pull: bool = False,
    loaded_profile: FbsIntegrationProfile | None = None,
) -> FbsIntegrationProfile:
    profile = loaded_profile
    if profile is not None and profile.pk != profile_id:
        raise FbsIntegrationError("Передан другой профиль FBS.")
    if profile is None:
        profile = (
            FbsIntegrationProfile.objects.select_related("agency")
            .filter(pk=profile_id)
            .first()
        )
    if profile is None:
        raise FbsIntegrationError("Профиль FBS не найден.")
    if not profile.is_active:
        raise FbsIntegrationError("Профиль FBS выключен.")
    if stream == FbsSyncCursor.STREAM_ORDERS:
        if allow_manual_order_pull and not feature_enabled("module"):
            raise FbsFeatureDisabled("Модуль FBS выключен.")
        if not allow_manual_order_pull and not feature_enabled("order_pull"):
            raise FbsFeatureDisabled("Получение заказов FBS выключено глобальным флагом.")
        if not profile.order_pull_enabled:
            raise FbsFeatureDisabled("Получение заказов выключено в профиле FBS.")
    elif stream == FbsSyncCursor.STREAM_STATUSES:
        if not feature_enabled("status_pull"):
            raise FbsFeatureDisabled("Получение статусов FBS выключено глобальным флагом.")
        if not profile.status_pull_enabled:
            raise FbsFeatureDisabled("Получение статусов выключено в профиле FBS.")
    else:
        raise FbsIntegrationError("Неизвестный поток синхронизации FBS.")
    return profile


def _acquire_cursor(profile: FbsIntegrationProfile, stream: str) -> tuple[int, str, dict, object]:
    FbsSyncCursor.objects.get_or_create(profile=profile, stream=stream, cursor_key="default")
    now = timezone.now()
    lease_seconds = max(int(getattr(settings, "FBS_SYNC_LEASE_SECONDS", 300)), 30)
    with transaction.atomic():
        cursor = FbsSyncCursor.objects.select_for_update().get(
            profile=profile,
            stream=stream,
            cursor_key="default",
        )
        if cursor.lease_token and cursor.lease_expires_at and cursor.lease_expires_at > now:
            raise FbsIntegrationError("Синхронизация этого профиля FBS уже выполняется.")
        token = uuid.uuid4().hex
        cursor.lease_token = token
        cursor.lease_expires_at = now + timedelta(seconds=lease_seconds)
        cursor.last_polled_at = now
        cursor.last_error = ""
        cursor.save(
            update_fields=[
                "lease_token",
                "lease_expires_at",
                "last_polled_at",
                "last_error",
                "updated_at",
            ]
        )
        return cursor.id, token, dict(cursor.cursor or {}), cursor.last_success_at


def _release_cursor(
    cursor_id: int,
    token: str,
    *,
    cursor_payload: dict | None = None,
    error: str = "",
) -> None:
    with transaction.atomic():
        cursor = FbsSyncCursor.objects.select_for_update().get(pk=cursor_id)
        if cursor.lease_token != token:
            return
        cursor.lease_token = ""
        cursor.lease_expires_at = None
        cursor.last_error = str(error or "")[:4000]
        update_fields = ["lease_token", "lease_expires_at", "last_error", "updated_at"]
        if cursor_payload is not None:
            cursor.cursor = cursor_payload
            update_fields.append("cursor")
        if not error:
            cursor.last_success_at = timezone.now()
            if cursor_payload is None:
                cursor.cursor = {}
                update_fields.append("cursor")
            update_fields.append("last_success_at")
        cursor.save(update_fields=update_fields)


def _require_json_response(response: MarketplaceHttpResponse, marketplace: str) -> dict:
    if not 200 <= response.status_code < 300:
        raise FbsIntegrationError(
            f"{marketplace} вернул HTTP {response.status_code} при чтении данных FBS."
        )
    if not isinstance(response.json_payload, dict):
        raise FbsIntegrationError(f"{marketplace} вернул некорректный JSON.")
    return response.json_payload


def _payload_hash(payload: dict) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


_SYNC_WRITE_BATCH_SIZE = 50


def _chunks(values: list, size: int = _SYNC_WRITE_BATCH_SIZE):
    batch_size = max(int(size), 1)
    for start in range(0, len(values), batch_size):
        yield values[start : start + batch_size]


def _wb_order_advertises_sgtin(order: NormalizedMarketplaceOrder) -> bool:
    for item in order.items:
        requirements = item.requirements if isinstance(item.requirements, dict) else {}
        for field in ("required_meta", "optional_meta"):
            values = requirements.get(field)
            if not isinstance(values, (list, tuple, set)):
                continue
            if any(str(value or "").strip().casefold() == "sgtin" for value in values):
                return True
    return False


def _read_wb_order_metadata(
    *,
    profile: FbsIntegrationProfile,
    orders: list[NormalizedMarketplaceOrder],
    transport: MarketplaceReadTransport,
) -> tuple[tuple[NormalizedMarketplaceOrder, ...], int]:
    candidates = [order for order in orders if _wb_order_advertises_sgtin(order)]
    if not candidates:
        return tuple(orders), 0

    numeric_ids = []
    for order in candidates:
        try:
            numeric_id = int(order.external_order_id)
        except (TypeError, ValueError) as exc:
            raise FbsIntegrationError(
                f"У заказа WB {order.external_order_id} некорректный числовой ID."
            ) from exc
        if numeric_id <= 0:
            raise FbsIntegrationError(
                f"У заказа WB {order.external_order_id} некорректный числовой ID."
            )
        numeric_ids.append(numeric_id)

    metadata_by_order: dict[str, dict[str, dict]] = {}
    request_count = 0
    for order_ids in _chunks(numeric_ids, 100):
        response = transport.send(profile, build_wb_orders_metadata_spec(order_ids))
        payload = _require_json_response(response, "WB")
        metadata_by_order.update(parse_wb_orders_metadata(payload))
        request_count += 1

    missing_ids = [
        order.external_order_id
        for order in candidates
        if str(order.external_order_id) not in metadata_by_order
    ]
    if missing_ids:
        sample = ", ".join(missing_ids[:5])
        raise FbsIntegrationError(
            f"WB не вернул метаданные {len(missing_ids)} заказов: {sample}."
        )
    return enrich_wb_orders_with_metadata(orders, metadata_by_order), request_count


def _event_key(event_type: str, external_id: str, payload_hash: str) -> tuple[str, str, str]:
    return event_type, external_id, payload_hash


def _prepare_marketplace_events(
    *,
    profile: FbsIntegrationProfile,
    event_type: str,
    payloads: list[tuple[str, dict]],
) -> dict[tuple[str, str, str], FbsMarketplaceEvent]:
    prepared_payloads = {}
    for external_id, payload in payloads:
        payload_hash = _payload_hash(payload)
        prepared_payloads[_event_key(event_type, external_id, payload_hash)] = payload
    if not prepared_payloads:
        return {}

    events_by_key = {}
    keys = list(prepared_payloads)
    for key_batch in _chunks(keys):
        query = Q()
        for _, external_id, payload_hash in key_batch:
            query |= Q(external_id=external_id, payload_hash=payload_hash)
        for event in FbsMarketplaceEvent.objects.filter(
            profile=profile,
            event_type=event_type,
        ).filter(query):
            events_by_key[
                _event_key(event.event_type, event.external_id, event.payload_hash)
            ] = event

    missing_keys = [key for key in keys if key not in events_by_key]
    if missing_keys:
        FbsMarketplaceEvent.objects.bulk_create(
            [
                FbsMarketplaceEvent(
                    profile=profile,
                    event_type=event_type,
                    external_id=external_id,
                    payload_hash=payload_hash,
                    payload=prepared_payloads[key],
                )
                for key in missing_keys
                for _, external_id, payload_hash in [key]
            ],
            batch_size=_SYNC_WRITE_BATCH_SIZE,
            ignore_conflicts=True,
        )
        for key_batch in _chunks(missing_keys):
            query = Q()
            for _, external_id, payload_hash in key_batch:
                query |= Q(external_id=external_id, payload_hash=payload_hash)
            for event in FbsMarketplaceEvent.objects.filter(
                profile=profile,
                event_type=event_type,
            ).filter(query):
                events_by_key[
                    _event_key(event.event_type, event.external_id, event.payload_hash)
                ] = event
    return events_by_key


def _event_for_payload(
    *,
    profile: FbsIntegrationProfile,
    event_type: str,
    external_id: str,
    payload: dict,
    prepared_events: dict[tuple[str, str, str], FbsMarketplaceEvent] | None = None,
) -> FbsMarketplaceEvent:
    payload_hash = _payload_hash(payload)
    key = _event_key(event_type, external_id, payload_hash)
    if prepared_events is not None:
        event = prepared_events.get(key)
        if event is None:
            raise FbsIntegrationError("Не удалось подготовить событие синхронизации FBS.")
        return event
    event, _ = FbsMarketplaceEvent.objects.get_or_create(
        profile=profile,
        event_type=event_type,
        external_id=external_id,
        payload_hash=payload_hash,
        defaults={"payload": payload},
    )
    return event


def _all_mapped_by_external_id(
    *,
    profile: FbsIntegrationProfile,
    external_order_ids: list[str],
) -> dict[str, bool]:
    unique_external_ids = tuple(dict.fromkeys(external_order_ids))
    if not unique_external_ids:
        return {}
    order_rows = list(
        FbsOrder.objects.filter(
            profile=profile,
            external_order_id__in=unique_external_ids,
        ).values_list("id", "external_order_id")
    )
    order_id_by_external = {external_id: order_id for order_id, external_id in order_rows}
    unmapped_order_ids = set(
        FbsOrderItem.objects.filter(
            order_id__in=order_id_by_external.values(),
            sku__isnull=True,
        ).values_list("order_id", flat=True)
    )
    return {
        external_id: (
            external_id not in order_id_by_external
            or order_id_by_external[external_id] not in unmapped_order_ids
        )
        for external_id in unique_external_ids
    }


def _parsed_datetime(value: str):
    parsed = parse_datetime(str(value or "").strip())
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def _select_internal_barcode(
    *,
    profile: FbsIntegrationProfile,
    sku: SKU,
    marketplace_barcodes: tuple[str, ...],
) -> tuple[str, str]:
    catalog_barcodes = list(
        SKUBarcode.objects.filter(sku=sku).order_by("id")
    )
    if not catalog_barcodes:
        return "", "barcode_missing"

    marketplace_values = {
        str(value or "").strip()
        for value in marketplace_barcodes
        if str(value or "").strip()
    }
    exact_rows = [
        catalog_barcode
        for catalog_barcode in catalog_barcodes
        if catalog_barcode.value in marketplace_values
    ]
    if len(exact_rows) == 1:
        return exact_rows[0].value, "matched"
    if len(exact_rows) > 1:
        exact_primary = [row for row in exact_rows if row.is_primary]
        if len(exact_primary) == 1:
            return exact_primary[0].value, "matched"

    candidate_values = {row.value for row in catalog_barcodes}
    stocked_values = set(
        FbsStockBalance.objects.filter(
            agency_id=profile.agency_id,
            sku_ref_id=sku.id,
            barcode__in=candidate_values,
            available_qty__gt=0,
        ).values_list("barcode", flat=True)
    )
    stocked_values.update(
        WarehouseStockSnapshot.objects.filter(
            agency_id=profile.agency_id,
            sku_ref_id=sku.id,
            barcode__in=candidate_values,
            is_archived=False,
            is_in_vehicle=False,
            active_operation__isnull=True,
            available_qty__gt=0,
        ).values_list("barcode", flat=True)
    )
    stocked_values &= candidate_values
    if len(stocked_values) == 1:
        return stocked_values.pop(), "matched"
    if len(stocked_values) > 1:
        stocked_primary = [
            row.value
            for row in catalog_barcodes
            if row.is_primary and row.value in stocked_values
        ]
        if len(stocked_primary) == 1:
            return stocked_primary[0], "matched"

    primary_barcodes = [row.value for row in catalog_barcodes if row.is_primary]
    if len(primary_barcodes) == 1:
        return primary_barcodes[0], "matched"
    if len(catalog_barcodes) == 1:
        return catalog_barcodes[0].value, "matched"
    return "", "barcode_ambiguous"


def _resolve_ozon_sku_binding(
    profile: FbsIntegrationProfile,
    item: NormalizedMarketplaceItem,
) -> tuple[SKU | None, str]:
    external_ids = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in item.binding_ids
            if str(value or "").strip()
        )
    )
    if not external_ids:
        return None, "ozon_sku_missing"

    bindings = list(
        MarketplaceBinding.objects.select_related("sku")
        .filter(
            marketplace=MarketplaceBinding.MARKETPLACE_OZON_SKU,
            external_id__in=external_ids,
            sku__agency_id=profile.agency_id,
            sku__deleted=False,
        )
        .order_by("id")
    )
    sku_ids = {binding.sku_id for binding in bindings}
    if len(sku_ids) == 1:
        return bindings[0].sku, "matched_ozon_sku"
    if len(sku_ids) > 1:
        return None, "ozon_sku_ambiguous"

    # A new Ozon product can reach FBS before the scheduled catalog sync.
    # Bootstrap only from the seller article, which is unique per client, and
    # persist the numeric Ozon SKU immediately for every following order.
    seller_article = str(item.external_sku or "").strip()
    sku = (
        SKU.objects.filter(
            agency_id=profile.agency_id,
            sku_code=seller_article,
            deleted=False,
        )
        .order_by("id")
        .first()
        if seller_article
        else None
    )
    if sku is None:
        return None, "ozon_sku_not_found"
    for external_id in external_ids:
        binding, _ = MarketplaceBinding.objects.get_or_create(
            marketplace=MarketplaceBinding.MARKETPLACE_OZON_SKU,
            external_id=external_id,
            defaults={
                "sku": sku,
                "last_synced_at": timezone.now(),
            },
        )
        if binding.sku_id != sku.id:
            return None, "ozon_sku_ambiguous"
    return sku, "matched_ozon_sku"


def _resolve_wb_sku_binding(
    profile: FbsIntegrationProfile,
    item: NormalizedMarketplaceItem,
) -> tuple[SKU | None, str]:
    """Resolve one client SKU from WB nmId, then from the seller article."""
    external_ids = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in (*item.binding_ids, item.external_sku)
            if str(value or "").strip()
        )
    )
    if external_ids:
        bindings = list(
            MarketplaceBinding.objects.select_related("sku")
            .filter(
                marketplace=MarketplaceBinding.MARKETPLACE_WB,
                external_id__in=external_ids,
                sku__agency_id=profile.agency_id,
                sku__deleted=False,
            )
            .order_by("id")
        )
        binding_sku_ids = {binding.sku_id for binding in bindings}
        if len(binding_sku_ids) == 1:
            return bindings[0].sku, "matched_wb_binding"
        if len(binding_sku_ids) > 1:
            return None, "wb_sku_ambiguous"

    seller_articles = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in item.sku_codes
            if str(value or "").strip()
        )
    )
    if not seller_articles:
        return None, "wb_sku_not_found"
    article_skus = list(
        SKU.objects.filter(
            agency_id=profile.agency_id,
            sku_code__in=seller_articles,
            deleted=False,
        ).order_by("id")
    )
    if len(article_skus) == 1:
        return article_skus[0], "matched_wb_article"
    if len(article_skus) > 1:
        return None, "wb_sku_ambiguous"
    return None, "wb_sku_not_found"


def _resolve_sku(
    profile: FbsIntegrationProfile,
    item: NormalizedMarketplaceItem,
) -> tuple[SKU | None, str, str]:
    barcodes = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in item.barcodes
            if str(value or "").strip()
        )
    )
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        sku, mapping_status = _resolve_ozon_sku_binding(profile, item)
        if sku is None:
            return None, mapping_status, ""
        barcode, barcode_status = _select_internal_barcode(
            profile=profile,
            sku=sku,
            marketplace_barcodes=barcodes,
        )
        if not barcode:
            return sku, barcode_status, ""
        return sku, mapping_status, barcode

    wb_sku = None
    wb_mapping_status = "wb_sku_not_found"
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        wb_sku, wb_mapping_status = _resolve_wb_sku_binding(profile, item)

    if not barcodes:
        if wb_sku is not None:
            barcode, barcode_status = _select_internal_barcode(
                profile=profile,
                sku=wb_sku,
                marketplace_barcodes=(),
            )
            if not barcode:
                return wb_sku, barcode_status, ""
            return wb_sku, wb_mapping_status, barcode
        return None, "barcode_missing", ""
    catalog_barcodes = list(
        SKUBarcode.objects.select_related("sku")
        .filter(
            value__in=barcodes,
            sku__agency_id=profile.agency_id,
            sku__deleted=False,
        )
        .order_by("id")
    )
    if not catalog_barcodes:
        if wb_sku is not None:
            barcode, barcode_status = _select_internal_barcode(
                profile=profile,
                sku=wb_sku,
                marketplace_barcodes=barcodes,
            )
            if not barcode:
                return wb_sku, barcode_status, ""
            return wb_sku, wb_mapping_status, barcode
        return (
            None,
            "barcode_not_found" if len(barcodes) == 1 else "barcode_ambiguous",
            barcodes[0] if len(barcodes) == 1 else "",
        )

    sku_ids = {catalog_barcode.sku_id for catalog_barcode in catalog_barcodes}
    if len(sku_ids) != 1:
        return None, "barcode_ambiguous", ""

    sku = catalog_barcodes[0].sku
    if wb_sku is not None and wb_sku.id != sku.id:
        return None, "wb_sku_ambiguous", ""
    if len(catalog_barcodes) == 1:
        return sku, "matched", catalog_barcodes[0].value

    # WB may send several labels for one chrtId. Only aliases of exactly
    # one catalog variant are interchangeable; a primary label or available
    # stock must never disambiguate different sizes.
    wb_same_variant = profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
    if wb_same_variant and len({_normalize_size(row.size) for row in catalog_barcodes}) != 1:
        return sku, "barcode_ambiguous", ""

    primary_barcodes = [
        catalog_barcode for catalog_barcode in catalog_barcodes if catalog_barcode.is_primary
    ]
    if len(primary_barcodes) == 1:
        return sku, "matched", primary_barcodes[0].value

    candidate_values = {catalog_barcode.value for catalog_barcode in catalog_barcodes}
    stocked_values = set(
        FbsStockBalance.objects.filter(
            agency_id=profile.agency_id,
            sku_ref_id=sku.id,
            barcode__in=candidate_values,
            available_qty__gt=0,
        ).values_list("barcode", flat=True)
    )
    stocked_values.update(
        WarehouseStockSnapshot.objects.filter(
            agency_id=profile.agency_id,
            sku_ref_id=sku.id,
            barcode__in=candidate_values,
            is_archived=False,
            is_in_vehicle=False,
            active_operation__isnull=True,
            available_qty__gt=0,
        ).values_list("barcode", flat=True)
    )
    stocked_values &= candidate_values
    if len(stocked_values) == 1:
        return sku, "matched", stocked_values.pop()

    if wb_same_variant:
        # All candidates are the same client's SKU and size. Reservation
        # already accepts their catalog aliases; keep a deterministic label
        # even when several aliases have stock (or stock has not arrived yet).
        return sku, "matched_barcode_alias", catalog_barcodes[0].value

    # SKU is known, but choosing one of several live barcodes would be unsafe.
    return sku, "barcode_ambiguous", ""


def _warehouse_matches(profile: FbsIntegrationProfile, order: NormalizedMarketplaceOrder) -> bool:
    return bool(order.warehouse_id) and order.warehouse_id == str(profile.external_warehouse_id).strip()


def _marketplace_order_is_cancelled(
    profile: FbsIntegrationProfile,
    *,
    status: str,
    substatus: str = "",
) -> bool:
    values = {
        str(status or "").strip().lower(),
        str(substatus or "").strip().lower(),
    }
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        return bool(
            values
            & {
                "cancel",
                "cancelled",
                "canceled",
                "cancel_missed_call",
                "canceled_by_client",
                "declined_by_client",
                "canceled_by_missed_call",
                "defect",
            }
        )
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        return any("cancel" in value for value in values if value)
    return False


def _marketplace_terminal_internal_status(
    profile: FbsIntegrationProfile,
    *,
    status: str,
    substatus: str = "",
) -> str:
    if profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        return ""
    normalized_status = str(status or "").strip().lower()
    normalized_substatus = str(substatus or "").strip().lower()
    if normalized_status != "complete":
        return ""
    if normalized_substatus == "sold":
        return FbsOrder.STATUS_DELIVERED
    return FbsOrder.STATUS_HANDED_OVER


def marketplace_terminal_order_q() -> Q:
    """Orders the marketplace has already closed, even if the local handover lags."""
    return Q(
        profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
        marketplace_status__iexact="complete",
    )


WB_CONFIRM_RECOVERY_STATE_FIELDS = (
    "has_unsafe_task",
    "has_physical_allocation",
    "has_bound_reservation",
    "has_unbound_reservation",
    "has_handover_assignment",
    "has_handover_order",
    "has_label",
    "has_marketplace_command",
)
_WB_CONFIRM_RECOVERY_STATE_UNSET = object()


def wb_confirm_order_recovery_states(
    orders,
) -> dict[int, dict[str, bool]]:
    """Load WB confirm recovery flags for a page of orders in one query."""
    eligible_statuses = {
        FbsOrder.STATUS_RECEIVED,
        FbsOrder.STATUS_AWAITING_STOCK,
        FbsOrder.STATUS_RESERVED,
    }
    order_ids = {
        int(order.pk)
        for order in orders
        if order.pk is not None and order.internal_status in eligible_statuses
    }
    if not order_ids:
        return {}

    rows = (
        FbsOrder.objects.filter(pk__in=order_ids)
        .annotate(
            has_unsafe_task=Exists(
                FbsPickTask.objects.filter(order_id=OuterRef("pk")).exclude(
                    status=FbsPickTask.STATUS_CANCELED,
                    picked_qty=0,
                )
            ),
            has_physical_allocation=Exists(
                FbsOrderStockAllocation.objects.filter(
                    order_item__order_id=OuterRef("pk")
                ).filter(
                    Q(
                        status__in=(
                            FbsOrderStockAllocation.STATUS_PICKING,
                            FbsOrderStockAllocation.STATUS_PICKED,
                        )
                    )
                    | Q(qty_picked__gt=0)
                )
            ),
            has_bound_reservation=Exists(
                FbsOrderStockAllocation.objects.filter(
                    order_item__order_id=OuterRef("pk"),
                    status=FbsOrderStockAllocation.STATUS_RESERVED,
                    pick_task__isnull=False,
                )
            ),
            has_unbound_reservation=Exists(
                FbsOrderStockAllocation.objects.filter(
                    order_item__order_id=OuterRef("pk"),
                    status=FbsOrderStockAllocation.STATUS_RESERVED,
                    pick_task__isnull=True,
                    qty_picked=0,
                )
            ),
            has_handover_assignment=Exists(
                FbsHandoverOrderAssignment.objects.filter(order_id=OuterRef("pk"))
            ),
            has_handover_order=Exists(
                FbsHandoverOrder.objects.filter(order_id=OuterRef("pk"))
            ),
            has_label=Exists(
                FbsOrderLabel.objects.filter(order_id=OuterRef("pk"))
            ),
            has_marketplace_command=Exists(
                FbsMarketplaceCommand.objects.filter(order_id=OuterRef("pk"))
            ),
        )
        .values("pk", *WB_CONFIRM_RECOVERY_STATE_FIELDS)
    )
    return {
        int(row["pk"]): {
            field: bool(row[field]) for field in WB_CONFIRM_RECOVERY_STATE_FIELDS
        }
        for row in rows
    }


def wb_confirm_order_recovery_error(
    order: FbsOrder,
    *,
    state=_WB_CONFIRM_RECOVERY_STATE_UNSET,
) -> str:
    """Return why an orphaned WB confirm order cannot safely re-enter a wave."""
    if order.internal_status not in {
        FbsOrder.STATUS_RECEIVED,
        FbsOrder.STATUS_AWAITING_STOCK,
        FbsOrder.STATUS_RESERVED,
    }:
        return "локальный заказ уже находится в другом этапе обработки"

    if state is _WB_CONFIRM_RECOVERY_STATE_UNSET:
        state = wb_confirm_order_recovery_states([order]).get(order.pk)
    if state is None:
        return "локальный заказ не найден"
    if state["has_unsafe_task"] or state["has_physical_allocation"]:
        return "по заказу уже есть задание или физический отбор"
    if state["has_bound_reservation"]:
        return "FBS-резерв уже привязан к предыдущему заданию"
    if state["has_handover_assignment"] or state["has_handover_order"]:
        return "заказ уже связан с локальной FBS-отгрузкой"
    if state["has_label"] or state["has_marketplace_command"]:
        return "по заказу уже запускалась этикетка или команда маркетплейса"
    if (
        order.internal_status == FbsOrder.STATUS_RESERVED
        and not state["has_unbound_reservation"]
    ):
        return "локальный статус резерва не подтвержден активным FBS-резервом"
    if (
        order.internal_status
        in {FbsOrder.STATUS_RECEIVED, FbsOrder.STATUS_AWAITING_STOCK}
        and state["has_unbound_reservation"]
    ):
        return "активный FBS-резерв не соответствует локальному статусу заказа"
    return ""


def marketplace_order_queue_error(
    order: FbsOrder,
    *,
    recovery_state=_WB_CONFIRM_RECOVERY_STATE_UNSET,
) -> str:
    """Return a user-facing reason when an order cannot enter a new pick wave."""
    if order.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        return ""
    status = str(order.marketplace_status or "").strip().lower()
    substatus = str(order.marketplace_substatus or "").strip().lower()
    if not status and not substatus:
        # Legacy/test rows can predate marketplace status synchronization.
        # The wave preflight still requires a fresh WB response before production use.
        return ""
    status_value = status or "не получен"
    substatus_value = substatus or "не получен"
    status_pair = f"supplierStatus={status_value}, wbStatus={substatus_value}"
    if _marketplace_order_is_cancelled(
        order.profile,
        status=status,
        substatus=substatus,
    ):
        return f"Заказ WB {order.external_order_id} отменен ({status_pair})."
    if status == "new":
        return ""
    if status == "confirm":
        if substatus and substatus != "waiting":
            explanation = "статус WB не допускает восстановление локальной волны"
        else:
            recovery_error = wb_confirm_order_recovery_error(
                order,
                state=recovery_state,
            )
            if not recovery_error:
                return ""
            explanation = recovery_error
    elif status == "complete":
        explanation = "заказ уже передан в доставку"
    elif not status:
        explanation = "актуальный статус WB не получен"
    else:
        explanation = "статус WB не допускает создание новой волны"
    return (
        f"Заказ WB {order.external_order_id} не включен в волну: "
        f"{status_pair}; {explanation}."
    )


def _register_stock_export_state(
    *,
    profile: FbsIntegrationProfile,
    item: NormalizedMarketplaceItem,
    sku: SKU,
    barcode: str,
) -> None:
    raw = item.raw_payload if isinstance(item.raw_payload, dict) else {}
    external_item_id = ""
    external_product_id = ""
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        external_item_id = str(raw.get("chrtId") or raw.get("chrtID") or "").strip()
        if external_item_id and not external_item_id.isdigit():
            raise FbsIntegrationError("WB вернул некорректный chrtId для выгрузки остатков.")
    elif profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        external_item_id = str(
            raw.get("offer_id")
            or raw.get("offerId")
            or raw.get("product_offer_id")
            or item.external_sku
            or ""
        ).strip()
        external_product_id = str(raw.get("product_id") or raw.get("sku") or "").strip()
    if not external_item_id:
        return
    state, _ = FbsStockExportState.objects.select_for_update().get_or_create(
        profile=profile,
        external_item_id=external_item_id,
        defaults={
            "sku_ref": sku,
            "barcode": barcode,
            "external_product_id": external_product_id,
        },
    )
    if state.sku_ref_id != sku.id:
        raise FbsIntegrationError(
            "Внешний товар для выгрузки остатков уже привязан к другому SKU клиента."
        )
    changed = []
    for field, value in (
        ("barcode", barcode),
        ("external_product_id", external_product_id),
    ):
        if value and getattr(state, field) != value:
            setattr(state, field, value)
            changed.append(field)
    if changed:
        state.full_clean(exclude={"id"})
        state.save(update_fields=[*changed, "updated_at"])


def _apply_imported_order_stock_state(order: FbsOrder, *, all_mapped: bool) -> None:
    from .picking import release_order_reservation, reserve_order_stock
    from .delivery_reconciliation import is_delivered

    # A delivered marketplace order with warehouse allocations must not release
    # unconsumed stock back to sale or be marked complete without debit evidence.
    # The status ingestion path reconciles fully consumed allocations below.
    if is_delivered(order.profile.marketplace, order.marketplace_status,
                    order.marketplace_substatus) and order.items.filter(
                        stock_allocations__isnull=False).exists():
        return

    if _marketplace_order_is_cancelled(
        order.profile,
        status=order.marketplace_status,
        substatus=order.marketplace_substatus,
    ):
        reservation_can_be_released = order.internal_status in {
            FbsOrder.STATUS_RESERVED,
            FbsOrder.STATUS_QUEUED_FOR_PICK,
        } or (
            order.internal_status == FbsOrder.STATUS_PICKING
            and not order.items.filter(stock_allocations__qty_picked__gt=0).exists()
        )
        if reservation_can_be_released:
            release_order_reservation(order_id=order.id, cancel_order=True)
        elif order.internal_status in {
            FbsOrder.STATUS_RECEIVED,
            FbsOrder.STATUS_VALIDATION_FAILED,
            FbsOrder.STATUS_AWAITING_STOCK,
        }:
            order.internal_status = FbsOrder.STATUS_CANCELLED
            order.hold_reason = ""
            order.save(update_fields=["internal_status", "hold_reason", "updated_at"])
        elif (
            order.internal_status
            in {
                FbsOrder.STATUS_PICKED,
                FbsOrder.STATUS_READY_FOR_HANDOVER,
                FbsOrder.STATUS_EXCEPTION,
            }
            and feature_enabled("module")
            and feature_enabled("warehouse_writes")
        ):
            from .pick_restock import ensure_cancelled_order_pick_restock

            ensure_cancelled_order_pick_restock(order_id=order.id)
        return
    terminal_status = _marketplace_terminal_internal_status(
        order.profile,
        status=order.marketplace_status,
        substatus=order.marketplace_substatus,
    )
    if terminal_status:
        reservation_can_be_released = order.internal_status in {
            FbsOrder.STATUS_RESERVED,
            FbsOrder.STATUS_QUEUED_FOR_PICK,
        } or (
            order.internal_status == FbsOrder.STATUS_PICKING
            and not order.items.filter(stock_allocations__qty_picked__gt=0).exists()
        )
        if reservation_can_be_released:
            release_order_reservation(order_id=order.id)
            order.refresh_from_db()
        if order.internal_status in {
            FbsOrder.STATUS_RECEIVED,
            FbsOrder.STATUS_VALIDATION_FAILED,
            FbsOrder.STATUS_AWAITING_STOCK,
            FbsOrder.STATUS_RESERVED,
            FbsOrder.STATUS_QUEUED_FOR_PICK,
        }:
            order.internal_status = terminal_status
            order.hold_reason = ""
            order.problem_reason = ""
            order.save(
                update_fields=[
                    "internal_status",
                    "hold_reason",
                    "problem_reason",
                    "updated_at",
                ]
            )
        return
    queue_error = marketplace_order_queue_error(order)
    if queue_error:
        marketplace_status = str(order.marketplace_status or "").strip().lower()
        active_pick_task_exists = order.pick_tasks.filter(
            status__in=(
                FbsPickTask.STATUS_QUEUED,
                FbsPickTask.STATUS_IN_PROGRESS,
            )
        ).exists()
        if (
            marketplace_status == "confirm"
            and active_pick_task_exists
            and order.internal_status
            in {
                FbsOrder.STATUS_QUEUED_FOR_PICK,
                FbsOrder.STATUS_PICKING,
            }
        ):
            if order.problem_reason:
                order.problem_reason = ""
                order.save(update_fields=["problem_reason", "updated_at"])
            return
        reservation_can_be_released = order.internal_status in {
            FbsOrder.STATUS_RESERVED,
            FbsOrder.STATUS_QUEUED_FOR_PICK,
        } or (
            order.internal_status == FbsOrder.STATUS_PICKING
            and not order.items.filter(stock_allocations__qty_picked__gt=0).exists()
        )
        if reservation_can_be_released:
            release_order_reservation(order_id=order.id)
            order.refresh_from_db()
        if order.internal_status in {
            FbsOrder.STATUS_RECEIVED,
            FbsOrder.STATUS_VALIDATION_FAILED,
            FbsOrder.STATUS_AWAITING_STOCK,
            FbsOrder.STATUS_RESERVED,
            FbsOrder.STATUS_QUEUED_FOR_PICK,
        }:
            order.problem_reason = queue_error
            order.save(update_fields=["problem_reason", "updated_at"])
        return
    if not all_mapped or not feature_enabled("warehouse_writes"):
        return
    if order.internal_status in {
        FbsOrder.STATUS_RECEIVED,
        FbsOrder.STATUS_AWAITING_STOCK,
        FbsOrder.STATUS_RESERVED,
    }:
        reserve_order_stock(order_id=order.id)


def _record_event_failure(event: FbsMarketplaceEvent, exc: Exception) -> None:
    event.status = FbsMarketplaceEvent.STATUS_FAILED
    event.attempt_count += 1
    event.error = str(exc)[:4000]
    event.save(update_fields=["status", "attempt_count", "error", "updated_at"])


def _ingest_order(
    profile: FbsIntegrationProfile,
    normalized: NormalizedMarketplaceOrder,
    *,
    prepared_events: dict[tuple[str, str, str], FbsMarketplaceEvent] | None = None,
    manage_transaction: bool = True,
) -> str:
    event_type = f"{profile.marketplace}_order"
    event = _event_for_payload(
        profile=profile,
        event_type=event_type,
        external_id=normalized.external_order_id,
        payload=normalized.raw_payload,
        prepared_events=prepared_events,
    )
    retry_validation = (
        profile.marketplace in {FbsIntegrationProfile.MARKETPLACE_OZON, FbsIntegrationProfile.MARKETPLACE_WB}
        and event.status == FbsMarketplaceEvent.STATUS_PROCESSED
        and FbsOrder.objects.filter(
            profile=profile,
            external_order_id=normalized.external_order_id,
            internal_status=FbsOrder.STATUS_VALIDATION_FAILED,
        ).exists()
    )
    if event.status == FbsMarketplaceEvent.STATUS_PROCESSED and not retry_validation:
        return "duplicate"
    try:
        transaction_context = transaction.atomic() if manage_transaction else nullcontext()
        with transaction_context:
            locked_event = FbsMarketplaceEvent.objects.select_for_update().get(pk=event.pk)
            if (
                locked_event.status == FbsMarketplaceEvent.STATUS_PROCESSED
                and not retry_validation
            ):
                return "duplicate"
            order, created = FbsOrder.objects.select_for_update().get_or_create(
                profile=profile,
                external_order_id=normalized.external_order_id,
                defaults={
                    "marketplace_status": normalized.marketplace_status,
                    "marketplace_substatus": normalized.marketplace_substatus,
                    "raw_payload": normalized.raw_payload,
                    "ordered_at": _parsed_datetime(normalized.ordered_at),
                    "cutoff_at": _parsed_datetime(normalized.cutoff_at),
                },
            )
            order.profile = profile
            changed = created
            field_values = {
                "marketplace_status": normalized.marketplace_status,
                "marketplace_substatus": normalized.marketplace_substatus,
                "raw_payload": normalized.raw_payload,
            }
            ordered_at = _parsed_datetime(normalized.ordered_at)
            cutoff_at = _parsed_datetime(normalized.cutoff_at)
            if ordered_at is not None:
                field_values["ordered_at"] = ordered_at
            if cutoff_at is not None:
                field_values["cutoff_at"] = cutoff_at
            update_fields = []
            for field, value in field_values.items():
                if getattr(order, field) != value:
                    setattr(order, field, value)
                    update_fields.append(field)

            mutable_composition = order.internal_status in {
                FbsOrder.STATUS_RECEIVED,
                FbsOrder.STATUS_VALIDATION_FAILED,
            }
            all_mapped = True
            incoming_line_ids = []
            for item in normalized.items:
                sku, mapping_status, barcode = _resolve_sku(profile, item)
                all_mapped = all_mapped and sku is not None and bool(barcode)
                requirements = dict(item.requirements or {})
                requirements["sku_mapping"] = mapping_status
                requirements["marketplace_barcodes"] = [
                    value
                    for value in dict.fromkeys(
                        str(raw or "").strip() for raw in item.barcodes
                    )
                    if value
                ]
                incoming_line_ids.append(item.external_line_id)
                existing_item = FbsOrderItem.objects.select_for_update().filter(
                    order=order,
                    external_line_id=item.external_line_id,
                ).first()
                if existing_item is not None and not mutable_composition:
                    composition_changed = (
                        existing_item.external_sku != item.external_sku
                        or existing_item.barcode != barcode
                        or existing_item.sku_id != (sku.id if sku else None)
                        or existing_item.quantity != item.quantity
                    )
                    if composition_changed:
                        raise FbsIntegrationError(
                            "Маркетплейс изменил состав заказа после начала складской обработки."
                        )
                if existing_item is None and not mutable_composition:
                    raise FbsIntegrationError(
                        "Маркетплейс добавил товар после начала складской обработки заказа."
                    )
                _, item_created = FbsOrderItem.objects.update_or_create(
                    order=order,
                    external_line_id=item.external_line_id,
                    defaults={
                        "external_sku": item.external_sku,
                        "barcode": barcode,
                        "sku": sku,
                        "product_name": item.product_name or (sku.name if sku else ""),
                        "quantity": item.quantity,
                        "requirements": requirements,
                        "raw_payload": item.raw_payload,
                    },
                )
                changed = changed or item_created
                if sku is not None and barcode:
                    _register_stock_export_state(
                        profile=profile,
                        item=item,
                        sku=sku,
                        barcode=barcode,
                    )

            stale_items = FbsOrderItem.objects.filter(order=order).exclude(
                external_line_id__in=incoming_line_ids
            )
            if stale_items.exists():
                if not mutable_composition or stale_items.filter(
                    stock_allocations__isnull=False
                ).exists():
                    raise FbsIntegrationError(
                        "Маркетплейс удалил товар после начала складской обработки заказа."
                    )
                stale_items.delete()
                changed = True

            if order.internal_status in {
                FbsOrder.STATUS_RECEIVED,
                FbsOrder.STATUS_VALIDATION_FAILED,
            }:
                desired_status = (
                    FbsOrder.STATUS_RECEIVED if all_mapped else FbsOrder.STATUS_VALIDATION_FAILED
                )
                if order.internal_status != desired_status:
                    order.internal_status = desired_status
                    update_fields.append("internal_status")
            if update_fields:
                order.save(update_fields=[*set(update_fields), "updated_at"])
                changed = True
            _apply_imported_order_stock_state(order, all_mapped=all_mapped)

            locked_event.payload = normalized.raw_payload
            locked_event.status = FbsMarketplaceEvent.STATUS_PROCESSED
            locked_event.attempt_count += 1
            locked_event.error = ""
            locked_event.processed_at = timezone.now()
            locked_event.save(
                update_fields=[
                    "payload",
                    "status",
                    "attempt_count",
                    "error",
                    "processed_at",
                    "updated_at",
                ]
            )
            return "created" if created else ("updated" if changed else "duplicate")
    except Exception as exc:
        if manage_transaction:
            event.refresh_from_db()
            _record_event_failure(event, exc)
        raise


def _ingest_status(
    profile: FbsIntegrationProfile,
    status_payload: dict,
    *,
    prepared_events: dict[tuple[str, str, str], FbsMarketplaceEvent] | None = None,
    all_mapped: bool | None = None,
    manage_transaction: bool = True,
) -> str:
    external_order_id = str(status_payload.get("external_order_id") or "").strip()
    raw_payload = status_payload.get("raw_payload")
    if not external_order_id or not isinstance(raw_payload, dict):
        raise FbsIntegrationError("Маркетплейс вернул неполный статус заказа.")
    event = _event_for_payload(
        profile=profile,
        event_type=f"{profile.marketplace}_status",
        external_id=external_order_id,
        payload=raw_payload,
        prepared_events=prepared_events,
    )

    def order_all_mapped(order: FbsOrder) -> bool:
        if all_mapped is not None:
            return all_mapped
        return not order.items.filter(sku__isnull=True).exists()

    def refresh_connected_handovers(
        order_id: int,
        *,
        include_accepted: bool = True,
    ) -> None:
        # Marketplace status is the only trigger; the refresh changes FBS
        # handover indicators, not stock, reservations, boxes, or documents.
        from .handover import refresh_handover_acceptance_for_order

        refresh_handover_acceptance_for_order(
            order_id=order_id,
            include_accepted=include_accepted,
        )

    duplicate_recovery_statuses = {
        FbsOrder.STATUS_RECEIVED,
        FbsOrder.STATUS_VALIDATION_FAILED,
        FbsOrder.STATUS_AWAITING_STOCK,
        FbsOrder.STATUS_RESERVED,
    }

    def reconcile_confirmed_delivery(order_id: int) -> None:
        from .delivery_reconciliation import is_delivered, reconcile_delivered_order

        if is_delivered(profile.marketplace, status_payload.get("marketplace_status"),
                        status_payload.get("marketplace_substatus")):
            reconcile_delivered_order(order_id=order_id, confirmation={
                **status_payload, "profile_id": profile.pk,
                "source": "marketplace_status_import",
            })

    def reconcile_duplicate_status() -> None:
        order_row = (
            FbsOrder.objects.filter(
                profile=profile,
                external_order_id=external_order_id,
            )
            .values_list("id", "internal_status")
            .first()
        )
        if order_row is None:
            return
        order_id, internal_status = order_row
        if internal_status in duplicate_recovery_statuses:
            transaction_context = transaction.atomic() if manage_transaction else nullcontext()
            with transaction_context:
                order = FbsOrder.objects.select_for_update().filter(pk=order_id).first()
                if order is not None:
                    order.profile = profile
                    _apply_imported_order_stock_state(
                        order,
                        all_mapped=order_all_mapped(order),
                    )
        # A dispatched/problem handover may have been created after the status
        # event. Accepted handovers are stable for an unchanged payload and do
        # not need to be rewritten on every marketplace poll.
        reconcile_confirmed_delivery(order_id)
        refresh_connected_handovers(order_id, include_accepted=False)

    if event.status == FbsMarketplaceEvent.STATUS_PROCESSED:
        reconcile_duplicate_status()
        return "duplicate"
    try:
        transaction_context = transaction.atomic() if manage_transaction else nullcontext()
        with transaction_context:
            locked_event = FbsMarketplaceEvent.objects.select_for_update().get(pk=event.pk)
            if locked_event.status == FbsMarketplaceEvent.STATUS_PROCESSED:
                reconcile_duplicate_status()
                return "duplicate"
            order = FbsOrder.objects.select_for_update().filter(
                profile=profile,
                external_order_id=external_order_id,
            ).first()
            if order is None:
                locked_event.status = FbsMarketplaceEvent.STATUS_PROCESSED
                locked_event.attempt_count += 1
                locked_event.processed_at = timezone.now()
                locked_event.error = "Заказ еще не импортирован в FBS."
                locked_event.save(
                    update_fields=[
                        "status",
                        "attempt_count",
                        "processed_at",
                        "error",
                        "updated_at",
                    ]
                )
                return "skipped"
            order.profile = profile
            status = str(status_payload.get("marketplace_status") or "").strip().lower()
            substatus = str(status_payload.get("marketplace_substatus") or "").strip().lower()
            changed = order.marketplace_status != status or order.marketplace_substatus != substatus
            order.marketplace_status = status
            order.marketplace_substatus = substatus
            raw_order = dict(order.raw_payload or {})
            raw_order["_latest_status"] = raw_payload
            order.raw_payload = raw_order
            order.save(
                update_fields=[
                    "marketplace_status",
                    "marketplace_substatus",
                    "raw_payload",
                    "updated_at",
                ]
            )
            _apply_imported_order_stock_state(
                order,
                all_mapped=order_all_mapped(order),
            )
            locked_event.status = FbsMarketplaceEvent.STATUS_PROCESSED
            locked_event.attempt_count += 1
            locked_event.error = ""
            locked_event.processed_at = timezone.now()
            locked_event.save(
                update_fields=[
                    "status",
                    "attempt_count",
                    "error",
                    "processed_at",
                    "updated_at",
                ]
            )
            reconcile_confirmed_delivery(order.id)
            refresh_connected_handovers(order.id)
            return "updated" if changed else "duplicate"
    except Exception as exc:
        if manage_transaction:
            event.refresh_from_db()
            _record_event_failure(event, exc)
        raise


def _record_batch_event_failure(event_id: int | None, exc: Exception) -> None:
    if event_id is None:
        return
    event = FbsMarketplaceEvent.objects.get(pk=event_id)
    if event.status != FbsMarketplaceEvent.STATUS_PROCESSED:
        _record_event_failure(event, exc)


def _ingest_orders_batch(
    profile: FbsIntegrationProfile,
    normalized_orders: list[NormalizedMarketplaceOrder],
) -> list[str]:
    if not normalized_orders:
        return []
    event_type = f"{profile.marketplace}_order"
    prepared_events = _prepare_marketplace_events(
        profile=profile,
        event_type=event_type,
        payloads=[
            (normalized.external_order_id, normalized.raw_payload)
            for normalized in normalized_orders
        ],
    )
    states = []
    for order_batch in _chunks(normalized_orders):
        failed_event_id = None
        try:
            with transaction.atomic():
                for normalized in order_batch:
                    event_key = _event_key(
                        event_type,
                        normalized.external_order_id,
                        _payload_hash(normalized.raw_payload),
                    )
                    failed_event_id = prepared_events[event_key].pk
                    states.append(
                        _ingest_order(
                            profile,
                            normalized,
                            prepared_events=prepared_events,
                            manage_transaction=False,
                        )
                    )
        except Exception as exc:
            _record_batch_event_failure(failed_event_id, exc)
            raise
    return states


def _ingest_statuses_batch(
    profile: FbsIntegrationProfile,
    status_payloads: list[dict],
) -> list[str]:
    if not status_payloads:
        return []
    normalized_payloads = []
    external_order_ids = []
    for status_payload in status_payloads:
        external_order_id = str(status_payload.get("external_order_id") or "").strip()
        raw_payload = status_payload.get("raw_payload")
        if not external_order_id or not isinstance(raw_payload, dict):
            raise FbsIntegrationError("Маркетплейс вернул неполный статус заказа.")
        external_order_ids.append(external_order_id)
        normalized_payloads.append((external_order_id, raw_payload))

    event_type = f"{profile.marketplace}_status"
    prepared_events = _prepare_marketplace_events(
        profile=profile,
        event_type=event_type,
        payloads=normalized_payloads,
    )
    mapped_by_external_id = _all_mapped_by_external_id(
        profile=profile,
        external_order_ids=external_order_ids,
    )
    states = []
    for status_batch in _chunks(status_payloads):
        failed_event_id = None
        try:
            with transaction.atomic():
                for status_payload in status_batch:
                    external_order_id = str(status_payload["external_order_id"]).strip()
                    raw_payload = status_payload["raw_payload"]
                    event_key = _event_key(
                        event_type,
                        external_order_id,
                        _payload_hash(raw_payload),
                    )
                    failed_event_id = prepared_events[event_key].pk
                    states.append(
                        _ingest_status(
                            profile,
                            status_payload,
                            prepared_events=prepared_events,
                            all_mapped=mapped_by_external_id.get(external_order_id),
                            manage_transaction=False,
                        )
                    )
        except Exception as exc:
            _record_batch_event_failure(failed_event_id, exc)
            raise
    return states


def _result_for_state(state: str) -> MarketplaceSyncResult:
    values = {"received": 1}
    if state in {"created", "updated", "duplicate", "skipped"}:
        values[state] = 1
    return MarketplaceSyncResult(**values)


def _rfc3339(value) -> str:
    return value.astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z")


def _pull_profile_orders(
    *,
    profile_id: int,
    transport: MarketplaceReadTransport | None = None,
    limit: int = 1000,
    allow_manual_order_pull: bool = False,
    loaded_profile: FbsIntegrationProfile | None = None,
) -> MarketplaceSyncResult:
    profile = _profile_for_sync(
        profile_id,
        stream=FbsSyncCursor.STREAM_ORDERS,
        allow_manual_order_pull=allow_manual_order_pull,
        loaded_profile=loaded_profile,
    )
    cursor_id, token, cursor_payload, last_success_at = _acquire_cursor(
        profile, FbsSyncCursor.STREAM_ORDERS
    )
    owned_transport = transport is None
    transport = transport or RequestsMarketplaceReadTransport(reuse_connections=True)
    result = MarketplaceSyncResult()
    next_cursor_payload = dict(cursor_payload)
    try:
        if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
            response = transport.send(profile, build_wb_new_orders_spec())
            payload = _require_json_response(response, "WB")
            result = MarketplaceSyncResult(requests=1)
            parsed_orders = parse_wb_new_orders(payload)
            accepted_orders = []
            skipped_orders = 0
            for order in parsed_orders:
                if not _warehouse_matches(profile, order):
                    skipped_orders += 1
                    continue
                accepted_orders.append(order)
            if skipped_orders:
                result = _add_results(
                    result,
                    MarketplaceSyncResult(
                        received=skipped_orders,
                        skipped=skipped_orders,
                    ),
                )
            live_external_ids = [
                order.external_order_id for order in accepted_orders
            ]
            existing_external_ids = set(
                FbsOrder.objects.filter(
                    profile=profile,
                    external_order_id__in=live_external_ids,
                ).values_list("external_order_id", flat=True)
            )
            # WB возвращает новые задания сразу по всем складам аккаунта. Раньше
            # лимит применялся до фильтрации склада: при 284 заданиях и лимите
            # 250 заказ нужного склада мог навсегда остаться за границей среза.
            # Сначала берем все задания нужного склада, затем ставим отсутствующие
            # локально вперед. Поэтому каждый следующий опрос гарантированно
            # забирает еще не импортированные заказы, даже при общем переполнении.
            missing_external_ids = set(live_external_ids) - existing_external_ids
            accepted_orders.sort(
                key=lambda order: (
                    order.external_order_id not in missing_external_ids,
                )
            )
            accepted_orders = accepted_orders[: max(int(limit), 1)]
            accepted_orders, metadata_requests = _read_wb_order_metadata(
                profile=profile,
                orders=accepted_orders,
                transport=transport,
            )
            result = _add_results(
                result,
                MarketplaceSyncResult(requests=metadata_requests),
            )
            for state in _ingest_orders_batch(profile, accepted_orders):
                result = _add_results(result, _result_for_state(state))
            local_external_ids_after = set(
                FbsOrder.objects.filter(
                    profile=profile,
                    external_order_id__in=live_external_ids,
                ).values_list("external_order_id", flat=True)
            )
            next_cursor_payload.update(
                {
                    "wb_live_total": len(parsed_orders),
                    "wb_profile_warehouse": len(live_external_ids),
                    "wb_missing_before": len(missing_external_ids),
                    "wb_missing_after": len(
                        set(live_external_ids) - local_external_ids_after
                    ),
                }
            )
        elif profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
            now = timezone.now()
            lookback_days = max(int(getattr(settings, "FBS_OZON_INITIAL_LOOKBACK_DAYS", 3)), 1)
            overlap_minutes = max(int(getattr(settings, "FBS_OZON_SYNC_OVERLAP_MINUTES", 60)), 5)
            pending_since = _parsed_datetime(cursor_payload.get("ozon_window_since"))
            pending_to = _parsed_datetime(cursor_payload.get("ozon_window_to"))
            pending_api_cursor = str(cursor_payload.get("ozon_api_cursor") or "").strip()
            if pending_since and pending_to and pending_api_cursor:
                since = pending_since
                window_to = pending_to
                api_cursor = pending_api_cursor
            else:
                completed_to = _parsed_datetime(cursor_payload.get("ozon_completed_to"))
                resume_from = completed_to or last_success_at
                since = (
                    resume_from - timedelta(minutes=overlap_minutes)
                    if resume_from
                    else now - timedelta(days=lookback_days)
                )
                window_to = now
                api_cursor = ""
            page_size = min(max(int(getattr(settings, "FBS_OZON_PAGE_SIZE", 100)), 1), 1000)
            remaining = max(int(limit), 1)
            while remaining > 0:
                current_limit = min(page_size, remaining)
                spec = build_ozon_postings_spec(
                    since=_rfc3339(since),
                    to=_rfc3339(window_to),
                    cursor=api_cursor,
                    limit=current_limit,
                )
                response = transport.send(profile, spec)
                payload = _require_json_response(response, "Ozon")
                postings, has_next, response_cursor = parse_ozon_postings(payload)
                result = _add_results(result, MarketplaceSyncResult(requests=1))
                accepted_orders = []
                for order in postings:
                    if not _warehouse_matches(profile, order):
                        result = _add_results(
                            result,
                            MarketplaceSyncResult(received=1, skipped=1),
                        )
                        continue
                    accepted_orders.append(order)
                for state in _ingest_orders_batch(profile, accepted_orders):
                    result = _add_results(result, _result_for_state(state))
                consumed = len(postings)
                remaining -= consumed
                if not has_next:
                    next_cursor_payload = {
                        key: value
                        for key, value in cursor_payload.items()
                        if key
                        not in {"ozon_window_since", "ozon_window_to", "ozon_api_cursor"}
                    }
                    next_cursor_payload["ozon_completed_to"] = _rfc3339(window_to)
                    break
                if consumed == 0:
                    raise FbsIntegrationError(
                        "Ozon вернул пустую страницу с признаком продолжения."
                    )
                if not response_cursor or response_cursor == api_cursor:
                    raise FbsIntegrationError("Ozon не вернул новый курсор следующей страницы.")
                api_cursor = response_cursor
                if remaining <= 0:
                    next_cursor_payload = {
                        **cursor_payload,
                        "ozon_window_since": _rfc3339(since),
                        "ozon_window_to": _rfc3339(window_to),
                        "ozon_api_cursor": api_cursor,
                    }
                    break
        else:
            raise FbsIntegrationError("Маркетплейс профиля FBS не поддерживается.")
        _release_cursor(
            cursor_id,
            token,
            cursor_payload={
                **next_cursor_payload,
                "last_received": result.received,
                "last_requests": result.requests,
            },
        )
        return result
    except Exception as exc:
        _release_cursor(cursor_id, token, error=str(exc))
        raise
    finally:
        if owned_transport:
            transport.close()


def pull_profile_orders(
    *,
    profile_id: int,
    transport: MarketplaceReadTransport | None = None,
    limit: int = 1000,
    loaded_profile: FbsIntegrationProfile | None = None,
) -> MarketplaceSyncResult:
    return _pull_profile_orders(
        profile_id=profile_id,
        transport=transport,
        limit=limit,
        loaded_profile=loaded_profile,
    )


def pull_profile_orders_manually(
    *,
    profile_id: int,
    actor,
    transport: MarketplaceReadTransport | None = None,
    limit: int = 1000,
    loaded_profile: FbsIntegrationProfile | None = None,
) -> MarketplaceSyncResult:
    profile = _profile_for_sync(
        profile_id,
        stream=FbsSyncCursor.STREAM_ORDERS,
        allow_manual_order_pull=True,
        loaded_profile=loaded_profile,
    )
    _require_manual_order_pull_actor(actor, agency_id=profile.agency_id)
    return _pull_profile_orders(
        profile_id=profile_id,
        transport=transport,
        limit=limit,
        allow_manual_order_pull=True,
        loaded_profile=profile,
    )


# Each lane has its own keyset cursor: archive growth cannot push live orders
# behind the complete history, while delivery/return reconciliation still runs.
_STATUS_HISTORY_STATES = (
    FbsOrder.STATUS_DELIVERED, FbsOrder.STATUS_CANCELLED, FbsOrder.STATUS_RETURNED,
)


def _status_cursor_key(order: FbsOrder) -> str:
    if order.internal_status in _STATUS_HISTORY_STATES:
        return "history_after_order_pk"
    if order.internal_status == FbsOrder.STATUS_HANDED_OVER:
        return "transit_after_order_pk"
    return "active_after_order_pk"


def _orders_after_cursor(profile: FbsIntegrationProfile, cursor_payload: dict, limit: int):
    limit = max(int(limit), 1)
    base = FbsOrder.objects.filter(profile=profile).order_by("pk")
    lanes = {
        "active_after_order_pk": base.exclude(
            internal_status__in=(*_STATUS_HISTORY_STATES, FbsOrder.STATUS_HANDED_OVER)
        ),
        "transit_after_order_pk": base.filter(internal_status=FbsOrder.STATUS_HANDED_OVER),
        "history_after_order_pk": base.filter(internal_status__in=_STATUS_HISTORY_STATES),
    }
    if limit >= 3:
        history = max(1, limit // 10)
        transit = max(1, limit // 5)
        budgets = (limit - history - transit, transit, history)
    else:
        # Tiny manual batches must not starve the other lanes either.
        turn = int(cursor_payload.get("status_poll_pass") or 0) % 10
        lane = 2 if turn == 9 else (1 if turn in (3, 7) else 0)
        budgets = tuple(limit if index == lane else 0 for index in range(3))
    selected = []

    def read_lane(key, count):
        if count <= 0:
            return []
        after_pk = max(int(cursor_payload.get(key) or 0), 0)
        queryset = lanes[key].exclude(pk__in=[row.pk for row in selected])
        rows = list(queryset.filter(pk__gt=after_pk)[:count])
        if len(rows) < count and after_pk:
            rows += list(queryset.filter(pk__lte=after_pk)[:count - len(rows)])
        return rows

    for (key, _queryset), count in zip(lanes.items(), budgets):
        selected.extend(read_lane(key, count))
    # Use spare capacity first for live orders. Each lane's reserved share is
    # retained even when a large live backlog is present.
    for key in lanes:
        if len(selected) >= limit:
            break
        selected.extend(read_lane(key, limit - len(selected)))
    return selected


def _advance_status_cursor(cursor_payload: dict, order: FbsOrder) -> None:
    cursor_payload[_status_cursor_key(order)] = order.pk
    cursor_payload["after_order_pk"] = order.pk


def refresh_wb_order_statuses_for_wave(
    *,
    order_ids: list[int] | tuple[int, ...],
    transport: MarketplaceReadTransport | None = None,
) -> tuple[int, ...]:
    """Refresh exactly the WB orders selected for a wave, without moving the sync cursor."""
    selected_order_ids = tuple(dict.fromkeys(int(order_id) for order_id in order_ids))
    if not selected_order_ids:
        return ()
    orders = list(
        FbsOrder.objects.select_related("profile__agency")
        .filter(
            pk__in=selected_order_ids,
            profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
        )
        .order_by("profile_id", "id")
    )
    if not orders:
        return ()
    transport = transport or RequestsMarketplaceReadTransport()
    orders_by_profile: dict[int, list[FbsOrder]] = {}
    for order in orders:
        orders_by_profile.setdefault(order.profile_id, []).append(order)

    pending_statuses: list[tuple[FbsIntegrationProfile, tuple[dict, ...]]] = []
    pending_status_by_order_id = {}
    refreshed_order_ids = []
    for profile_id, profile_orders in orders_by_profile.items():
        profile = _profile_for_sync(
            profile_id,
            stream=FbsSyncCursor.STREAM_STATUSES,
            loaded_profile=profile_orders[0].profile,
        )
        external_to_order = {}
        numeric_order_ids = []
        for order in profile_orders:
            try:
                external_id = int(order.external_order_id)
            except (TypeError, ValueError) as exc:
                raise FbsIntegrationError(
                    f"У заказа WB {order.external_order_id} некорректный числовой ID."
                ) from exc
            external_to_order[str(external_id)] = order
            numeric_order_ids.append(external_id)
        if len(numeric_order_ids) > 1000:
            raise FbsIntegrationError(
                "WB разрешает проверить не более 1000 заказов за один запрос."
            )
        response = transport.send(profile, build_wb_statuses_spec(numeric_order_ids))
        payload = _require_json_response(response, "WB")
        statuses = tuple(
            status_payload
            for status_payload in parse_wb_statuses(payload)
            if status_payload["external_order_id"] in external_to_order
        )
        pending_statuses.append((profile, statuses))
        pending_status_by_order_id.update(
            {
                external_to_order[status_payload["external_order_id"]].id: status_payload
                for status_payload in statuses
            }
        )
        refreshed_order_ids.extend(
            external_to_order[status_payload["external_order_id"]].id
            for status_payload in statuses
        )

    refreshed_order_ids = tuple(dict.fromkeys(refreshed_order_ids))
    mapped_by_order_id = {}
    for profile_orders in orders_by_profile.values():
        mapped_by_external_id = _all_mapped_by_external_id(
            profile=profile_orders[0].profile,
            external_order_ids=[order.external_order_id for order in profile_orders],
        )
        mapped_by_order_id.update(
            {
                order.id: mapped_by_external_id.get(order.external_order_id, False)
                for order in profile_orders
            }
        )
    with transaction.atomic():
        for profile, statuses in pending_statuses:
            _ingest_statuses_batch(profile, list(statuses))

        refreshed_orders = (
            FbsOrder.objects.select_for_update()
            .select_related("profile")
            .filter(pk__in=refreshed_order_ids)
        )
        for order in refreshed_orders:
            status_payload = pending_status_by_order_id[order.id]
            status = str(status_payload["marketplace_status"] or "").strip().lower()
            substatus = str(status_payload["marketplace_substatus"] or "").strip().lower()
            raw_order = dict(order.raw_payload or {})
            raw_order["_latest_status"] = status_payload["raw_payload"]
            order.marketplace_status = status
            order.marketplace_substatus = substatus
            order.raw_payload = raw_order
            order.save(
                update_fields=[
                    "marketplace_status",
                    "marketplace_substatus",
                    "raw_payload",
                    "updated_at",
                ]
            )
            _apply_imported_order_stock_state(
                order,
                all_mapped=mapped_by_order_id.get(order.id, False),
            )
    return refreshed_order_ids


def pull_profile_statuses(
    *,
    profile_id: int,
    transport: MarketplaceReadTransport | None = None,
    limit: int = 100,
    loaded_profile: FbsIntegrationProfile | None = None,
) -> MarketplaceSyncResult:
    profile = _profile_for_sync(
        profile_id,
        stream=FbsSyncCursor.STREAM_STATUSES,
        loaded_profile=loaded_profile,
    )
    cursor_id, token, cursor_payload, _ = _acquire_cursor(profile, FbsSyncCursor.STREAM_STATUSES)
    owned_transport = transport is None
    transport = transport or RequestsMarketplaceReadTransport(reuse_connections=True)
    result = MarketplaceSyncResult()
    requested_limit = max(int(limit), 1)
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        requested_limit = min(
            requested_limit,
            max(int(getattr(settings, "FBS_OZON_STATUS_BATCH_LIMIT", 100)), 1),
        )
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        requested_limit = min(requested_limit, 1000)
    next_cursor = dict(cursor_payload)
    try:
        orders = _orders_after_cursor(profile, cursor_payload, requested_limit)
        if not orders:
            _release_cursor(cursor_id, token, cursor_payload=next_cursor)
            return result
        if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
            numeric_orders = []
            for order in orders[:1000]:
                try:
                    numeric_orders.append(int(order.external_order_id))
                except (TypeError, ValueError):
                    result = _add_results(result, MarketplaceSyncResult(skipped=1))
            if numeric_orders:
                response = transport.send(profile, build_wb_statuses_spec(numeric_orders))
                payload = _require_json_response(response, "WB")
                result = _add_results(result, MarketplaceSyncResult(requests=1))
                status_payloads = list(parse_wb_statuses(payload))
                for state in _ingest_statuses_batch(profile, status_payloads):
                    result = _add_results(
                        result,
                        _result_for_state(state),
                    )
        elif profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
            for order in orders:
                response = transport.send(
                    profile,
                    build_ozon_posting_status_spec(order.external_order_id),
                )
                payload = _require_json_response(response, "Ozon")
                status_payload = parse_ozon_posting_status(payload)
                result = _add_results(result, MarketplaceSyncResult(requests=1))
                result = _add_results(
                    result,
                    _result_for_state(_ingest_status(profile, status_payload)),
                )
                _advance_status_cursor(next_cursor, order)
        else:
            raise FbsIntegrationError("Маркетплейс профиля FBS не поддерживается.")
        for order in orders:
            _advance_status_cursor(next_cursor, order)
        next_cursor["status_poll_pass"] = (int(cursor_payload.get("status_poll_pass") or 0) + 1) % 10
        _release_cursor(cursor_id, token, cursor_payload=next_cursor)
        return result
    except Exception as exc:
        _release_cursor(cursor_id, token, cursor_payload=next_cursor, error=str(exc))
        raise
    finally:
        if owned_transport:
            transport.close()
