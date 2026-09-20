from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from math import ceil

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from fbs.exceptions import FbsFeatureDisabled, FbsIntegrationError
from fbs.flags import feature_enabled
from fbs.integrations.contracts import MarketplaceReadSpec
from fbs.integrations.http import (
    MarketplaceHttpResponse,
    MarketplaceReadTransport,
    RequestsMarketplaceReadTransport,
)
from fbs.integrations.ozon import build_ozon_stock_update_spec
from fbs.integrations.wb import (
    build_wb_cards_spec,
    build_wb_stock_update_spec,
    parse_wb_card_stock_bindings,
)
from fbs.models import (
    FbsClientMovementRequest,
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsReplenishmentAllocation,
    FbsReplenishmentPlan,
    FbsStockBalance,
    FbsStockExportState,
)
from sklad.models import WarehouseEvent
from sku.models import MarketplaceBinding

from .physical_locations import fbs_box_reservable_q

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StockExportResult:
    profiles: int = 0
    inspected: int = 0
    requests: int = 0
    synced: int = 0
    retry: int = 0
    blocked: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class _ClaimedStock:
    state_id: int
    generation: int
    desired_qty: int
    external_item_id: str
    external_product_id: str


@dataclass(frozen=True)
class _CatalogRefreshResult:
    requests: int = 0
    mapped: int = 0
    blocked: int = 0


@dataclass(frozen=True)
class _RemoteStockReconcileResult:
    requests: int = 0
    inspected: int = 0
    matched: int = 0
    requeued: int = 0
    skipped: int = 0


WB_UNMAPPED_PREFIX = "wb-unmapped:"
CLIENT_MOVEMENT_EXPORT_EVENT = "fbs_client_movement_stock_export_activated"
CLIENT_MOVEMENT_EXPORT_CONTEXT = "fbs_client_movement"
WB_STOCK_RECONCILE_BATCH_SIZE = 1000


def _is_ozon_marketplace_barcode(value: str) -> bool:
    return str(value or "").strip().casefold().startswith("ozn")


def _apply_safety_stock(available: dict[str, int], safety_stock_qty: int) -> dict[str, int]:
    safety_stock_qty = max(int(safety_stock_qty or 0), 0)
    return {
        key: max(int(quantity or 0) - safety_stock_qty, 0)
        for key, quantity in available.items()
    }


def _activated_client_movement_request_ids(profile: FbsIntegrationProfile) -> tuple[int, ...]:
    warehouse_confirmed_ids = set(
        FbsClientMovementRequest.objects.filter(
            agency_id=profile.agency_id,
            warehouse_confirmed_at__isnull=False,
        ).values_list("id", flat=True)
    )
    request_ids: list[int] = []
    values = WarehouseEvent.objects.filter(
        agency_id=profile.agency_id,
        event_type=CLIENT_MOVEMENT_EXPORT_EVENT,
        stock_context_type=CLIENT_MOVEMENT_EXPORT_CONTEXT,
    ).values_list("stock_context_id", flat=True)
    for value in values:
        try:
            request_id = int(str(value or "").strip())
        except (TypeError, ValueError):
            continue
        if request_id > 0 and request_id in warehouse_confirmed_ids:
            request_ids.append(request_id)
    return tuple(request_ids)


def _add_results(left: StockExportResult, right: StockExportResult) -> StockExportResult:
    return StockExportResult(
        profiles=left.profiles + right.profiles,
        inspected=left.inspected + right.inspected,
        requests=left.requests + right.requests,
        synced=left.synced + right.synced,
        retry=left.retry + right.retry,
        blocked=left.blocked + right.blocked,
        skipped=left.skipped + right.skipped,
    )


def _require_stock_push() -> None:
    if not feature_enabled("stock_push"):
        raise FbsFeatureDisabled("Выгрузка FBS-остатков выключена глобальным флагом.")


def _enabled_profile(profile_id: int) -> FbsIntegrationProfile:
    profile = FbsIntegrationProfile.objects.select_related("agency").filter(pk=profile_id).first()
    if profile is None:
        raise FbsIntegrationError("Профиль FBS не найден.")
    if (
        not profile.is_active
        or not profile.stock_push_enabled
        or profile.stock_mode != FbsIntegrationProfile.STOCK_MODE_MANAGED
    ):
        raise FbsFeatureDisabled("Выгрузка остатков выключена для этого склада клиента.")
    return profile


def _wb_placeholder_id(barcode: str) -> str:
    return f"{WB_UNMAPPED_PREFIX}{str(barcode or '').strip()}"


def _wb_catalog_search_text(state: FbsStockExportState) -> str:
    for binding in state.sku_ref.marketplace_bindings.all():
        marketplace = str(binding.marketplace or "").strip().upper()
        external_id = str(binding.external_id or "").strip()
        if marketplace in {"WB", "WILDBERRIES"} and external_id:
            return external_id
    return str(state.sku_ref.sku_code or "").strip()


def _wb_catalog_min_interval_seconds() -> float:
    try:
        configured = float(
            getattr(settings, "FBS_STOCK_CATALOG_MIN_INTERVAL_SECONDS", 0.65)
        )
    except (TypeError, ValueError):
        configured = 0.65
    return max(configured, 0.6)


def _wb_catalog_retry_seconds(response: MarketplaceHttpResponse) -> int | None:
    headers = {
        str(key).strip().lower(): str(value).strip()
        for key, value in response.headers.items()
    }
    for header in ("x-ratelimit-retry", "retry-after", "x-ratelimit-reset"):
        raw_value = headers.get(header, "")
        try:
            seconds = float(raw_value)
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            return max(ceil(seconds) + 1, 1)
    return None


def _pending_client_movement_allocations(profile: FbsIntegrationProfile):
    """Return pending quantities only for warehouse-confirmed legacy requests."""
    request_ids = _activated_client_movement_request_ids(profile)
    queryset = (
        FbsReplenishmentAllocation.objects.filter(
            line__plan__agency_id=profile.agency_id,
            line__plan__client_movement_request__isnull=False,
            line__plan__status__in=(
                FbsReplenishmentPlan.STATUS_CONFIRMED,
                FbsReplenishmentPlan.STATUS_IN_PROGRESS,
                FbsReplenishmentPlan.STATUS_AWAITING_PACK,
            ),
            status__in=(
                FbsReplenishmentAllocation.STATUS_RESERVED,
                FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
                FbsReplenishmentAllocation.STATUS_STAGED,
            ),
            stock_movement__isnull=True,
        )
        .select_related("source_snapshot")
        .order_by("id")
    )
    if not request_ids:
        return queryset.none()
    return queryset.filter(line__plan__client_movement_request_id__in=request_ids)


@transaction.atomic
def _ensure_wb_stock_catalog_states(profile: FbsIntegrationProfile) -> int:
    if profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        return 0
    balance_rows = list(
        FbsStockBalance.objects.filter(
            agency_id=profile.agency_id,
            sku_ref__isnull=False,
        )
        .filter(fbs_box_reservable_q())
        .exclude(barcode="")
        .order_by()
        .values("sku_ref_id", "barcode")
        .distinct()
    )
    pending_rows = list(
        _pending_client_movement_allocations(profile)
        .filter(source_snapshot__sku_ref__isnull=False)
        .exclude(source_snapshot__barcode="")
        .order_by()
        .values(
            sku_ref_id=F("source_snapshot__sku_ref_id"),
            barcode=F("source_snapshot__barcode"),
        )
        .distinct()
    )
    existing = set(
        FbsStockExportState.objects.filter(profile=profile).values_list(
            "sku_ref_id", "barcode"
        )
    )
    now = timezone.now()
    created = 0
    for row in [*balance_rows, *pending_rows]:
        key = (row["sku_ref_id"], str(row["barcode"] or "").strip())
        if not key[1] or _is_ozon_marketplace_barcode(key[1]) or key in existing:
            continue
        _, was_created = FbsStockExportState.objects.get_or_create(
            profile=profile,
            external_item_id=_wb_placeholder_id(key[1]),
            defaults={
                "sku_ref_id": key[0],
                "barcode": key[1],
                "status": FbsStockExportState.STATUS_BLOCKED,
                "next_attempt_at": now,
                "error": "Ожидается сопоставление chrtID WB по штрихкоду.",
            },
        )
        if was_created:
            created += 1
            existing.add(key)
    return created


@transaction.atomic
def _ensure_ozon_stock_catalog_states(profile: FbsIntegrationProfile) -> int:
    """Bootstrap Ozon exports from this client's synced Ozon nomenclature.

    Offer IDs belong to the seller account, while the destination warehouse is
    taken only from the explicitly enabled profile when the update is sent.
    """
    if profile.marketplace != FbsIntegrationProfile.MARKETPLACE_OZON:
        return 0
    balance_sku_ids = (
        FbsStockBalance.objects.filter(
            agency_id=profile.agency_id,
            sku_ref__isnull=False,
        )
        .filter(fbs_box_reservable_q())
        .order_by()
        .values_list("sku_ref_id", flat=True)
        .distinct()
    )
    pending_sku_ids = (
        _pending_client_movement_allocations(profile)
        .filter(source_snapshot__sku_ref__isnull=False)
        .order_by()
        .values_list("source_snapshot__sku_ref_id", flat=True)
        .distinct()
    )
    sku_ids = {
        int(sku_id)
        for sku_id in [*balance_sku_ids, *pending_sku_ids]
        if sku_id
    }
    if not sku_ids:
        return 0

    bindings_by_sku: dict[int, MarketplaceBinding] = {}
    bindings = (
        MarketplaceBinding.objects.select_related("sku")
        .filter(
            sku_id__in=sku_ids,
            sku__agency_id=profile.agency_id,
            sku__deleted=False,
            marketplace__iexact="OZON",
        )
        .order_by("sku_id", "id")
    )
    for binding in bindings:
        bindings_by_sku.setdefault(binding.sku_id, binding)

    created = 0
    for binding in bindings_by_sku.values():
        sku = binding.sku
        offer_id = str(sku.sku_code or "").strip()
        if not offer_id:
            continue
        product_id = str(binding.external_id or "").strip()
        if product_id and not product_id.isdigit():
            product_id = ""
        state, was_created = FbsStockExportState.objects.get_or_create(
            profile=profile,
            external_item_id=offer_id,
            defaults={
                "sku_ref": sku,
                "external_product_id": product_id,
            },
        )
        if was_created:
            created += 1
            continue
        if state.sku_ref_id != sku.id:
            continue
        if product_id and state.external_product_id != product_id:
            state.external_product_id = product_id
            state.status = FbsStockExportState.STATUS_PENDING
            state.error = ""
            state.next_attempt_at = timezone.now()
            state.save(
                update_fields=[
                    "external_product_id",
                    "status",
                    "error",
                    "next_attempt_at",
                    "updated_at",
                ]
            )
    return created


@transaction.atomic
def _mark_wb_catalog_blocked(
    *,
    state_ids: list[int],
    error: str,
    retry_seconds: int | None = None,
) -> int:
    if retry_seconds is None:
        retry_seconds = max(
            int(getattr(settings, "FBS_STOCK_CATALOG_RETRY_SECONDS", 600)),
            60,
        )
    else:
        retry_seconds = max(int(retry_seconds), 1)
    states = list(
        FbsStockExportState.objects.select_for_update().filter(id__in=state_ids)
    )
    next_attempt_at = timezone.now() + timedelta(seconds=retry_seconds)
    for state in states:
        state.status = FbsStockExportState.STATUS_BLOCKED
        state.error = str(error or "WB-карточка не сопоставлена.")[:2000]
        state.next_attempt_at = next_attempt_at
        state.save(update_fields=["status", "error", "next_attempt_at", "updated_at"])
    return len(states)


@transaction.atomic
def _apply_wb_catalog_binding(*, state_id: int, chrt_id: str) -> bool:
    state = (
        FbsStockExportState.objects.select_for_update()
        .select_related("profile")
        .get(pk=state_id)
    )
    existing = (
        FbsStockExportState.objects.select_for_update()
        .filter(profile=state.profile, external_item_id=chrt_id)
        .exclude(pk=state.pk)
        .first()
    )
    if existing is not None:
        if existing.sku_ref_id != state.sku_ref_id:
            state.status = FbsStockExportState.STATUS_BLOCKED
            state.error = "chrtID WB уже привязан к другому SKU клиента."
            state.next_attempt_at = None
            state.save(update_fields=["status", "error", "next_attempt_at", "updated_at"])
            return False
        changed = []
        if not existing.barcode:
            existing.barcode = state.barcode
            changed.append("barcode")
        elif existing.barcode != state.barcode:
            available = _available_by_binding(state.profile)
            existing_qty = int(available.get(str(existing.barcode or "").strip(), 0))
            incoming_qty = int(available.get(str(state.barcode or "").strip(), 0))
            if existing_qty > 0 and incoming_qty > 0:
                state.status = FbsStockExportState.STATUS_BLOCKED
                state.error = (
                    "Один chrtID WB сопоставлен нескольким штрихкодам с остатком."
                )
                state.next_attempt_at = None
                state.save(
                    update_fields=["status", "error", "next_attempt_at", "updated_at"]
                )
                return False
            if incoming_qty > 0 and existing_qty <= 0:
                existing.barcode = state.barcode
                changed.append("barcode")
        if changed:
            existing.save(update_fields=[*changed, "updated_at"])
        state.delete()
        return True
    state.external_item_id = chrt_id
    state.status = FbsStockExportState.STATUS_PENDING
    state.error = ""
    state.next_attempt_at = timezone.now()
    state.save(
        update_fields=[
            "external_item_id",
            "status",
            "error",
            "next_attempt_at",
            "updated_at",
        ]
    )
    return True


def _refresh_wb_stock_catalog(
    *,
    profile: FbsIntegrationProfile,
    transport: MarketplaceReadTransport,
) -> _CatalogRefreshResult:
    if profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        return _CatalogRefreshResult()
    now = timezone.now()
    limit = min(
        max(int(getattr(settings, "FBS_STOCK_CATALOG_BATCH_LIMIT", 50)), 1),
        100,
    )
    states = list(
        FbsStockExportState.objects.select_related("sku_ref")
        .prefetch_related("sku_ref__marketplace_bindings")
        .filter(
            profile=profile,
            external_item_id__startswith=WB_UNMAPPED_PREFIX,
        )
        .exclude(
            Q(barcode__istartswith="OZN")
            | Q(external_item_id__istartswith=f"{WB_UNMAPPED_PREFIX}OZN")
        )
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("id")[:limit]
    )
    by_search_text: dict[str, list[FbsStockExportState]] = defaultdict(list)
    for state in states:
        by_search_text[_wb_catalog_search_text(state)].append(state)
    requests = 0
    mapped = 0
    blocked = 0
    last_request_started_at: float | None = None
    grouped_items = list(by_search_text.items())
    for group_index, (search_text, grouped_states) in enumerate(grouped_items):
        state_ids = [state.id for state in grouped_states]
        if not search_text:
            blocked += _mark_wb_catalog_blocked(
                state_ids=state_ids,
                error="Для поиска карточки WB у SKU отсутствует внешний ID и артикул клиента.",
            )
            continue
        try:
            if last_request_started_at is not None:
                wait_seconds = _wb_catalog_min_interval_seconds() - (
                    time.monotonic() - last_request_started_at
                )
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
            last_request_started_at = time.monotonic()
            response = transport.send(
                profile,
                build_wb_cards_spec(text_search=search_text),
            )
            requests += 1
            if response.status_code == 429:
                remaining_ids = [
                    state.id
                    for _, pending_states in grouped_items[group_index:]
                    for state in pending_states
                ]
                blocked += _mark_wb_catalog_blocked(
                    state_ids=remaining_ids,
                    error="WB Content API временно ограничил частоту запросов (HTTP 429).",
                    retry_seconds=_wb_catalog_retry_seconds(response),
                )
                break
            if not 200 <= response.status_code < 300:
                raise FbsIntegrationError(
                    f"WB Content API вернул HTTP {response.status_code}."
                )
            bindings = parse_wb_card_stock_bindings(
                response.json_payload,
                barcodes={state.barcode for state in grouped_states},
            )
        except Exception as exc:
            blocked += _mark_wb_catalog_blocked(
                state_ids=state_ids,
                error=str(exc),
            )
            continue
        for state in grouped_states:
            chrt_ids = bindings.get(state.barcode, ())
            if len(chrt_ids) != 1:
                reason = (
                    "WB не нашел карточку с точным штрихкодом."
                    if not chrt_ids
                    else "WB вернул несколько chrtID для одного штрихкода."
                )
                blocked += _mark_wb_catalog_blocked(
                    state_ids=[state.id],
                    error=reason,
                )
                continue
            if _apply_wb_catalog_binding(state_id=state.id, chrt_id=chrt_ids[0]):
                mapped += 1
            else:
                blocked += 1
    return _CatalogRefreshResult(requests=requests, mapped=mapped, blocked=blocked)


def _available_by_binding(profile: FbsIntegrationProfile) -> dict[str, int]:
    from .client_profiles import client_safety_stock_qty
    from .inventory import with_fbs_lock_state

    balances = with_fbs_lock_state(
        FbsStockBalance.objects.filter(
            agency_id=profile.agency_id,
            available_qty__gt=0,
        )
        .filter(fbs_box_reservable_q())
        .select_related(
            "box__pallet__cell",
            "box__source_container__current_location",
        ),
        ignore_internal_movement=True,
    )
    by_barcode: dict[str, int] = defaultdict(int)
    by_sku: dict[str, int] = defaultdict(int)
    for balance in balances:
        if balance._fbs_is_locked:
            continue
        by_barcode[str(balance.barcode or "").strip()] += int(balance.available_qty or 0)
        if balance.sku_ref_id:
            by_sku[str(balance.sku_ref_id)] += int(balance.available_qty or 0)
    for allocation in _pending_client_movement_allocations(profile):
        snapshot = allocation.source_snapshot
        qty = max(
            int(allocation.qty_planned or 0) - int(allocation.qty_moved or 0),
            0,
        )
        if qty <= 0:
            continue
        by_barcode[str(snapshot.barcode or "").strip()] += qty
        if snapshot.sku_ref_id:
            by_sku[str(snapshot.sku_ref_id)] += qty
    available = (
        by_barcode
        if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        else by_sku
    )
    return _apply_safety_stock(
        available,
        client_safety_stock_qty(agency_id=profile.agency_id),
    )


@transaction.atomic
def queue_agency_stock_exports(*, agency_id: int) -> int:
    """Refresh export rows after an FBS client-movement state change.

    This performs no network request.  It only makes the exact desired quantity
    pending for the regular marketplace stock exporter.
    """
    if not feature_enabled("stock_push"):
        return 0
    profiles = list(
        FbsIntegrationProfile.objects.filter(
            agency_id=int(agency_id),
            is_active=True,
            stock_push_enabled=True,
            stock_mode=FbsIntegrationProfile.STOCK_MODE_MANAGED,
        ).order_by("id")
    )
    changed = 0
    for profile in profiles:
        if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
            _ensure_wb_stock_catalog_states(profile)
        elif profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
            _ensure_ozon_stock_catalog_states(profile)
        changed += refresh_profile_stock_export_states(profile_id=profile.id)
    return changed


@transaction.atomic
def refresh_profile_stock_export_states(*, profile_id: int) -> int:
    profile = _enabled_profile(profile_id)
    available = _available_by_binding(profile)
    states = list(
        FbsStockExportState.objects.select_for_update()
        .filter(profile=profile)
        .order_by("id")
    )
    now = timezone.now()
    changed_count = 0
    for state in states:
        if state.external_item_id.startswith(WB_UNMAPPED_PREFIX):
            continue
        lookup_key = (
            str(state.barcode or "").strip()
            if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
            else str(state.sku_ref_id)
        )
        desired_qty = max(int(available.get(lookup_key, 0)), 0)
        desired_changed = state.desired_qty != desired_qty
        needs_first_sync = state.last_sent_qty is None
        if not desired_changed and not needs_first_sync:
            continue
        state.desired_qty = desired_qty
        if desired_changed:
            state.generation = int(state.generation or 0) + 1
        state.status = FbsStockExportState.STATUS_PENDING
        state.error = ""
        if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
            state.next_attempt_at = now
        elif state.next_attempt_at is None or state.next_attempt_at < now:
            state.next_attempt_at = now
        state.save(
            update_fields=[
                "desired_qty",
                "generation",
                "status",
                "error",
                "next_attempt_at",
                "updated_at",
            ]
        )
        changed_count += 1
    return changed_count


def _batch_limit(profile: FbsIntegrationProfile) -> int:
    return 1000 if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB else 100


def _wb_stock_reconcile_interval() -> timedelta:
    try:
        seconds = int(getattr(settings, "FBS_WB_STOCK_RECONCILE_SECONDS", 300))
    except (TypeError, ValueError):
        seconds = 300
    return timedelta(seconds=max(seconds, 60))


def _wb_stock_read_spec(
    *,
    profile: FbsIntegrationProfile,
    external_item_ids: list[int],
) -> MarketplaceReadSpec:
    return MarketplaceReadSpec(
        operation="wb_reconcile_fbs_stocks",
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint=f"/api/v3/stocks/{profile.external_warehouse_id}",
        endpoint_version="v3",
        query={},
        body={"chrtIds": external_item_ids},
    )


def _read_wb_remote_stocks(
    *,
    profile: FbsIntegrationProfile,
    states: list[FbsStockExportState],
    transport: MarketplaceReadTransport,
) -> tuple[dict[str, int], int]:
    external_item_ids = sorted(
        {
            int(state.external_item_id)
            for state in states
            if str(state.external_item_id or "").isdigit()
        }
    )
    live: dict[str, int] = {}
    requests = 0
    for offset in range(0, len(external_item_ids), WB_STOCK_RECONCILE_BATCH_SIZE):
        batch = external_item_ids[offset : offset + WB_STOCK_RECONCILE_BATCH_SIZE]
        response = transport.send(
            profile,
            _wb_stock_read_spec(profile=profile, external_item_ids=batch),
        )
        requests += 1
        if not 200 <= int(response.status_code or 0) < 300:
            raise FbsIntegrationError(
                f"Wildberries вернул HTTP {response.status_code} при сверке остатков."
            )
        payload = response.json_payload
        rows = payload.get("stocks") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise FbsIntegrationError(
                "Wildberries не вернул список фактических остатков при сверке."
            )
        for row in rows:
            if not isinstance(row, dict):
                continue
            external_item_id = str(row.get("chrtId") or "").strip()
            if not external_item_id.isdigit():
                continue
            try:
                amount = int(row.get("amount") or 0)
            except (TypeError, ValueError) as exc:
                raise FbsIntegrationError(
                    "Wildberries вернул некорректное количество при сверке остатков."
                ) from exc
            live[external_item_id] = max(amount, 0)
    return live, requests


def _reconcile_wb_remote_stock(
    *,
    profile: FbsIntegrationProfile,
    transport: MarketplaceReadTransport,
    force: bool = False,
) -> _RemoteStockReconcileResult:
    if profile.marketplace != FbsIntegrationProfile.MARKETPLACE_WB:
        return _RemoteStockReconcileResult()

    candidates = (
        FbsStockExportState.objects.filter(
            profile=profile,
            status=FbsStockExportState.STATUS_SYNCED,
            last_sent_qty=F("desired_qty"),
        )
        .exclude(external_item_id__startswith=WB_UNMAPPED_PREFIX)
        .order_by("id")
    )
    if not force:
        cutoff = timezone.now() - _wb_stock_reconcile_interval()
        candidates = candidates.filter(
            Q(last_success_at__isnull=True) | Q(last_success_at__lte=cutoff)
        )
    states = [
        state
        for state in candidates
        if str(state.external_item_id or "").isdigit()
    ]
    if not states:
        return _RemoteStockReconcileResult()

    live, requests = _read_wb_remote_stocks(
        profile=profile,
        states=states,
        transport=transport,
    )
    snapshot = {
        state.id: (
            int(state.generation or 0),
            int(state.desired_qty or 0),
            str(state.external_item_id or ""),
        )
        for state in states
    }
    now = timezone.now()
    matched = 0
    requeued = 0
    skipped = 0
    with transaction.atomic():
        locked = FbsStockExportState.objects.select_for_update().filter(
            profile=profile,
            id__in=list(snapshot),
        )
        for state in locked:
            generation, desired_qty, external_item_id = snapshot[state.id]
            # WB may omit an item that has no row on the seller warehouse.
            # For stock reconciliation that is equivalent to a remote zero.
            remote_qty = live.get(external_item_id, 0)
            if (
                state.status != FbsStockExportState.STATUS_SYNCED
                or int(state.generation or 0) != generation
                or int(state.desired_qty or 0) != desired_qty
            ):
                skipped += 1
                continue
            if remote_qty == desired_qty:
                state.last_success_at = now
                state.save(update_fields=["last_success_at", "updated_at"])
                matched += 1
                continue
            state.status = FbsStockExportState.STATUS_PENDING
            state.next_attempt_at = now
            state.error = (
                "Фактический остаток Wildberries "
                f"{remote_qty} шт., в Fullbox {desired_qty} шт. "
                "Запланирована повторная передача."
            )
            state.response_payload = {
                "wb_reconciliation": {
                    "checked_at": now.isoformat(),
                    "remote_qty": remote_qty,
                    "desired_qty": desired_qty,
                }
            }
            state.save(
                update_fields=[
                    "status",
                    "next_attempt_at",
                    "error",
                    "response_payload",
                    "updated_at",
                ]
            )
            requeued += 1
    return _RemoteStockReconcileResult(
        requests=requests,
        inspected=len(states),
        matched=matched,
        requeued=requeued,
        skipped=skipped,
    )


def _claim_delay(profile: FbsIntegrationProfile) -> timedelta:
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        return timedelta(
            seconds=max(int(getattr(settings, "FBS_OZON_STOCK_MIN_INTERVAL_SECONDS", 120)), 120)
        )
    return timedelta(seconds=max(int(getattr(settings, "FBS_STOCK_CLAIM_SECONDS", 30)), 10))


@transaction.atomic
def _claim_due_states(
    *,
    profile: FbsIntegrationProfile,
    limit: int,
) -> tuple[_ClaimedStock, ...]:
    now = timezone.now()
    stale = FbsStockExportState.objects.select_for_update().filter(
        profile=profile,
        status=FbsStockExportState.STATUS_SENDING,
        next_attempt_at__lte=now,
    )
    stale.update(status=FbsStockExportState.STATUS_RETRY, error="Предыдущая попытка не завершилась.")
    states = list(
        FbsStockExportState.objects.select_for_update()
        .filter(
            profile=profile,
            status__in=(
                FbsStockExportState.STATUS_PENDING,
                FbsStockExportState.STATUS_RETRY,
            ),
        )
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        # A stale positive marketplace stock can create an order that the
        # warehouse cannot fulfil. Publish zeroes before ordinary quantity
        # changes whenever a batch is limited.
        .order_by("desired_qty", "next_attempt_at", "id")[: max(int(limit), 1)]
    )
    next_attempt_at = now + _claim_delay(profile)
    claimed = []
    for state in states:
        state.status = FbsStockExportState.STATUS_SENDING
        state.attempt_count = int(state.attempt_count or 0) + 1
        state.last_attempt_at = now
        state.next_attempt_at = next_attempt_at
        state.error = ""
        state.save(
            update_fields=[
                "status",
                "attempt_count",
                "last_attempt_at",
                "next_attempt_at",
                "error",
                "updated_at",
            ]
        )
        claimed.append(
            _ClaimedStock(
                state_id=state.id,
                generation=int(state.generation or 0),
                desired_qty=int(state.desired_qty or 0),
                external_item_id=state.external_item_id,
                external_product_id=state.external_product_id,
            )
        )
    return tuple(claimed)


def _spec_for(profile: FbsIntegrationProfile, rows: tuple[_ClaimedStock, ...]):
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        return build_wb_stock_update_spec(
            warehouse_id=profile.external_warehouse_id,
            stocks=[
                {"chrtId": row.external_item_id, "amount": row.desired_qty}
                for row in rows
            ],
        )
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        return build_ozon_stock_update_spec(
            warehouse_id=profile.external_warehouse_id,
            stocks=[
                {
                    "offer_id": row.external_item_id,
                    "product_id": row.external_product_id,
                    "stock": row.desired_qty,
                }
                for row in rows
            ],
        )
    raise FbsIntegrationError("Маркетплейс профиля FBS не поддерживается.")


def _response_payload(response: MarketplaceHttpResponse):
    return response.json_payload if isinstance(response.json_payload, (dict, list)) else {}


def _retry_delay(attempt_count: int) -> timedelta:
    return timedelta(seconds=min(10 * (2 ** max(attempt_count - 1, 0)), 300))


@transaction.atomic
def _mark_batch_retry(
    *,
    profile: FbsIntegrationProfile,
    rows: tuple[_ClaimedStock, ...],
    error: str,
    response: MarketplaceHttpResponse | None = None,
) -> int:
    now = timezone.now()
    count = 0
    states = {
        state.id: state
        for state in FbsStockExportState.objects.select_for_update().filter(
            profile=profile,
            id__in=[row.state_id for row in rows],
        )
    }
    for row in rows:
        state = states.get(row.state_id)
        if state is None:
            continue
        state.status = FbsStockExportState.STATUS_RETRY
        state.error = str(error or "Временная ошибка выгрузки остатков.")[:2000]
        if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
            state.next_attempt_at = now + _retry_delay(state.attempt_count)
        if response is not None:
            state.last_http_status = response.status_code
            state.response_payload = _response_payload(response)
        state.save(
            update_fields=[
                "status",
                "error",
                "next_attempt_at",
                "last_http_status",
                "response_payload",
                "updated_at",
            ]
        )
        count += 1
    return count


@transaction.atomic
def _mark_batch_blocked(
    *,
    profile: FbsIntegrationProfile,
    rows: tuple[_ClaimedStock, ...],
    error: str,
) -> int:
    count = 0
    states = {
        state.id: state
        for state in FbsStockExportState.objects.select_for_update().filter(
            profile=profile,
            id__in=[row.state_id for row in rows],
        )
    }
    for row in rows:
        state = states.get(row.state_id)
        if state is None:
            continue
        state.status = FbsStockExportState.STATUS_BLOCKED
        state.error = str(error or "Некорректные данные выгрузки остатков.")[:2000]
        state.next_attempt_at = None
        state.save(update_fields=["status", "error", "next_attempt_at", "updated_at"])
        count += 1
    return count


@transaction.atomic
def _mark_rows_success(
    *,
    profile: FbsIntegrationProfile,
    rows: tuple[_ClaimedStock, ...],
    response: MarketplaceHttpResponse,
) -> int:
    now = timezone.now()
    count = 0
    states = {
        state.id: state
        for state in FbsStockExportState.objects.select_for_update().filter(
            profile=profile,
            id__in=[row.state_id for row in rows],
        )
    }
    for row in rows:
        state = states.get(row.state_id)
        if state is None:
            continue
        if int(state.generation or 0) == row.generation:
            state.last_sent_qty = row.desired_qty
            state.status = FbsStockExportState.STATUS_SYNCED
        else:
            state.status = FbsStockExportState.STATUS_PENDING
        state.last_success_at = now
        state.last_http_status = response.status_code
        state.response_payload = _response_payload(response)
        state.error = ""
        if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
            state.next_attempt_at = None
        state.save(
            update_fields=[
                "last_sent_qty",
                "status",
                "last_success_at",
                "last_http_status",
                "response_payload",
                "error",
                "next_attempt_at",
                "updated_at",
            ]
        )
        count += 1
    return count


def _ozon_result_rows(response: MarketplaceHttpResponse) -> dict[str, dict]:
    payload = response.json_payload if isinstance(response.json_payload, dict) else {}
    result = payload.get("result")
    if not isinstance(result, list):
        raise FbsIntegrationError("Ozon не вернул результаты обновления остатков.")
    return {
        str(row.get("offer_id") or "").strip(): row
        for row in result
        if isinstance(row, dict) and str(row.get("offer_id") or "").strip()
    }


def _apply_ozon_response(
    *,
    profile: FbsIntegrationProfile,
    rows: tuple[_ClaimedStock, ...],
    response: MarketplaceHttpResponse,
) -> StockExportResult:
    result_rows = _ozon_result_rows(response)
    successful = []
    retry = []
    blocked = []
    for row in rows:
        result = result_rows.get(row.external_item_id)
        errors = result.get("errors") if isinstance(result, dict) else None
        errors = errors if isinstance(errors, list) else []
        if result and bool(result.get("updated")) and not errors:
            successful.append(row)
            continue
        codes = {
            str(error.get("code") or "").strip().upper()
            for error in errors
            if isinstance(error, dict)
        }
        if "TOO_MANY_REQUESTS" in codes or not result:
            retry.append(row)
        else:
            blocked.append((row, errors or [{"message": "Ozon не подтвердил обновление."}]))
    synced = _mark_rows_success(
        profile=profile,
        rows=tuple(successful),
        response=response,
    )
    retry_count = _mark_batch_retry(
        profile=profile,
        rows=tuple(retry),
        error="Ozon временно не принял обновление остатков.",
        response=response,
    )
    blocked_count = 0
    for row, errors in blocked:
        with transaction.atomic():
            state = FbsStockExportState.objects.select_for_update().get(pk=row.state_id)
            state.status = FbsStockExportState.STATUS_BLOCKED
            state.error = str(errors)[:2000]
            state.last_http_status = response.status_code
            state.response_payload = _response_payload(response)
            state.save(
                update_fields=[
                    "status",
                    "error",
                    "last_http_status",
                    "response_payload",
                    "updated_at",
                ]
            )
            blocked_count += 1
    return StockExportResult(
        inspected=len(rows),
        requests=1,
        synced=synced,
        retry=retry_count,
        blocked=blocked_count,
    )


def sync_profile_stock_exports(
    *,
    profile_id: int,
    transport: MarketplaceReadTransport | None = None,
    limit: int | None = None,
) -> StockExportResult:
    _require_stock_push()
    profile = _enabled_profile(profile_id)
    read_transport = transport or RequestsMarketplaceReadTransport()
    catalog_result = _CatalogRefreshResult()
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        _ensure_wb_stock_catalog_states(profile)
        catalog_result = _refresh_wb_stock_catalog(
            profile=profile,
            transport=read_transport,
        )
    elif profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        _ensure_ozon_stock_catalog_states(profile)
    base_result = StockExportResult(
        profiles=1,
        requests=catalog_result.requests,
        blocked=catalog_result.blocked,
    )
    refresh_profile_stock_export_states(profile_id=profile.id)
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        try:
            reconciliation = _reconcile_wb_remote_stock(
                profile=profile,
                transport=read_transport,
            )
        except Exception:
            logger.exception(
                "Не удалось сверить фактические остатки WB для FBS-профиля %s.",
                profile.id,
            )
        else:
            base_result = _add_results(
                base_result,
                StockExportResult(requests=reconciliation.requests),
            )
    batch_limit = min(max(int(limit or _batch_limit(profile)), 1), _batch_limit(profile))
    rows = _claim_due_states(profile=profile, limit=batch_limit)
    if not rows:
        return _add_results(base_result, StockExportResult(skipped=1))
    try:
        spec = _spec_for(profile, rows)
    except FbsIntegrationError as exc:
        blocked = _mark_batch_blocked(profile=profile, rows=rows, error=str(exc))
        return _add_results(base_result, StockExportResult(
            inspected=len(rows),
            blocked=blocked,
        ))
    try:
        response = read_transport.send(
            profile,
            spec,
        )
    except Exception as exc:
        retry = _mark_batch_retry(profile=profile, rows=rows, error=str(exc))
        return _add_results(base_result, StockExportResult(
            inspected=len(rows),
            requests=1,
            retry=retry,
        ))
    if not 200 <= response.status_code < 300:
        retry = _mark_batch_retry(
            profile=profile,
            rows=rows,
            error=f"Маркетплейс вернул HTTP {response.status_code}.",
            response=response,
        )
        return _add_results(base_result, StockExportResult(
            inspected=len(rows),
            requests=1,
            retry=retry,
        ))
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        try:
            result = _apply_ozon_response(profile=profile, rows=rows, response=response)
        except FbsIntegrationError as exc:
            retry = _mark_batch_retry(
                profile=profile,
                rows=rows,
                error=str(exc),
                response=response,
            )
            return _add_results(base_result, StockExportResult(
                inspected=len(rows),
                requests=1,
                retry=retry,
            ))
        return _add_results(base_result, result)
    synced = _mark_rows_success(profile=profile, rows=rows, response=response)
    return _add_results(base_result, StockExportResult(
        inspected=len(rows),
        requests=1,
        synced=synced,
    ))


def sync_enabled_stock_exports(
    *,
    transport: MarketplaceReadTransport | None = None,
    profile_ids: tuple[int, ...] = (),
) -> StockExportResult:
    _require_stock_push()
    profiles = FbsIntegrationProfile.objects.filter(
        is_active=True,
        stock_push_enabled=True,
        stock_mode=FbsIntegrationProfile.STOCK_MODE_MANAGED,
    ).order_by("id")
    if profile_ids:
        profiles = profiles.filter(id__in=profile_ids)
    result = StockExportResult()
    for profile_id in profiles.values_list("id", flat=True):
        result = _add_results(
            result,
            sync_profile_stock_exports(profile_id=profile_id, transport=transport),
        )
    return result
