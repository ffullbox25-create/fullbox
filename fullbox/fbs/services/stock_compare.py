from __future__ import annotations

from typing import Any

from django.utils import timezone

from fbs.exceptions import FbsIntegrationError
from fbs.integrations.contracts import MarketplaceReadSpec
from fbs.integrations.http import (
    MarketplaceReadTransport,
    RequestsMarketplaceReadTransport,
)
from fbs.models import (
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsStockBalance,
    FbsStockExportState,
)
from sku.models import SKU

from .stock_sync import WB_UNMAPPED_PREFIX, _available_by_binding


WB_STOCK_CHUNK_SIZE = 1000
OZON_STOCK_PAGE_SIZE = 1000
OZON_STOCK_MAX_PAGES = 100


def _require_success(response, marketplace_label: str) -> dict:
    if not 200 <= int(response.status_code or 0) < 300:
        raise FbsIntegrationError(
            f"{marketplace_label} вернул ошибку HTTP {response.status_code}."
        )
    payload = response.json_payload
    if not isinstance(payload, dict):
        raise FbsIntegrationError(
            f"{marketplace_label} вернул ответ в неизвестном формате."
        )
    return payload


def _read_wb_stocks(
    profile: FbsIntegrationProfile,
    states: list[FbsStockExportState],
    transport: MarketplaceReadTransport,
) -> dict[str, int]:
    chrt_ids = sorted(
        {
            int(state.external_item_id)
            for state in states
            if str(state.external_item_id or "").isdigit()
        }
    )
    live: dict[str, int] = {}
    for offset in range(0, len(chrt_ids), WB_STOCK_CHUNK_SIZE):
        response = transport.send(
            profile,
            MarketplaceReadSpec(
                operation="fbs_stock_comparison_wb",
                http_method=FbsMarketplaceCommand.METHOD_POST,
                endpoint=f"/api/v3/stocks/{profile.external_warehouse_id}",
                endpoint_version="v3",
                query={},
                body={"chrtIds": chrt_ids[offset : offset + WB_STOCK_CHUNK_SIZE]},
            ),
        )
        payload = _require_success(response, "Wildberries")
        rows = payload.get("stocks")
        if not isinstance(rows, list):
            raise FbsIntegrationError(
                "Wildberries не вернул список остатков склада продавца."
            )
        for row in rows:
            if not isinstance(row, dict):
                continue
            external_id = str(row.get("chrtId") or "").strip()
            if external_id:
                live[external_id] = max(int(row.get("amount") or 0), 0)
    return live


def _read_ozon_fbs_stocks(
    profile: FbsIntegrationProfile,
    transport: MarketplaceReadTransport,
) -> dict[str, int]:
    live: dict[str, int] = {}
    cursor = ""
    for _ in range(OZON_STOCK_MAX_PAGES):
        body: dict[str, Any] = {
            "filter": {"visibility": "ALL"},
            "limit": OZON_STOCK_PAGE_SIZE,
        }
        if cursor:
            body["cursor"] = cursor
        response = transport.send(
            profile,
            MarketplaceReadSpec(
                operation="fbs_stock_comparison_ozon",
                http_method=FbsMarketplaceCommand.METHOD_POST,
                endpoint="/v4/product/info/stocks",
                endpoint_version="v4",
                query={},
                body=body,
            ),
        )
        payload = _require_success(response, "Ozon")
        items = payload.get("items")
        if not isinstance(items, list):
            raise FbsIntegrationError("Ozon не вернул список остатков товаров.")
        for item in items:
            if not isinstance(item, dict):
                continue
            offer_id = str(item.get("offer_id") or "").strip()
            if not offer_id:
                continue
            live[offer_id] = sum(
                max(int(stock.get("present") or 0), 0)
                for stock in (item.get("stocks") or [])
                if isinstance(stock, dict)
                and str(stock.get("type") or "").strip().lower() == "fbs"
            )
        next_cursor = str(payload.get("cursor") or "").strip()
        if not items or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    else:
        raise FbsIntegrationError("Ozon вернул слишком много страниц остатков.")
    return live


def _local_metadata(
    *,
    profile: FbsIntegrationProfile,
    available: dict[str, int],
) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        sku_ids = [int(key) for key in available if str(key).isdigit()]
        for sku in SKU.objects.filter(
            agency_id=profile.agency_id,
            id__in=sku_ids,
        ).only("id", "sku_code", "name"):
            metadata[str(sku.id)] = {
                "sku_code": str(sku.sku_code or ""),
                "name": str(sku.name or ""),
                "barcode": "",
            }
        return metadata

    rows = (
        FbsStockBalance.objects.filter(
            agency_id=profile.agency_id,
            barcode__in=list(available),
        )
        .order_by("barcode", "id")
        .values("barcode", "sku_code", "name")
    )
    for row in rows:
        key = str(row["barcode"] or "").strip()
        if key and key not in metadata:
            metadata[key] = {
                "sku_code": str(row["sku_code"] or ""),
                "name": str(row["name"] or ""),
                "barcode": key,
            }
    return metadata


def _profile_rows(
    profile: FbsIntegrationProfile,
    *,
    transport: MarketplaceReadTransport,
) -> tuple[list[dict], dict]:
    states = list(
        FbsStockExportState.objects.filter(profile=profile)
        .select_related("sku_ref")
        .order_by("sku_ref__sku_code", "barcode", "external_item_id", "id")
    )
    available = _available_by_binding(profile)
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        live = _read_wb_stocks(profile, states, transport)
        warehouse_note = f"склад продавца {profile.external_warehouse_id}"
    else:
        live = _read_ozon_fbs_stocks(profile, transport)
        warehouse_note = "все FBS-склады кабинета Ozon"

    local_metadata = _local_metadata(profile=profile, available=available)
    used_keys: set[str] = set()
    mapped_keys: set[str] = set()
    rows: list[dict] = []
    for state in states:
        local_key = (
            str(state.barcode or "").strip()
            if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
            else str(state.sku_ref_id)
        )
        used_keys.add(local_key)
        external_id = str(state.external_item_id or "").strip()
        mapping_missing = (
            not external_id
            or external_id.startswith(WB_UNMAPPED_PREFIX)
            or (
                profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
                and not external_id.isdigit()
            )
        )
        if not mapping_missing:
            mapped_keys.add(local_key)
        fullbox_qty = max(int(available.get(local_key, 0)), 0)
        # A stale zero-balance placeholder is not a stock discrepancy and should
        # not turn an otherwise exact marketplace profile red.
        if mapping_missing and fullbox_qty <= 0:
            continue
        marketplace_qty = None if mapping_missing else max(int(live.get(external_id, 0)), 0)
        difference = (
            None
            if marketplace_qty is None
            else marketplace_qty - fullbox_qty
        )
        rows.append(
            {
                "profile_id": profile.id,
                "profile_name": profile.name,
                "marketplace": profile.marketplace,
                "marketplace_label": profile.get_marketplace_display(),
                "warehouse_note": warehouse_note,
                "sku_code": str(state.sku_ref.sku_code or ""),
                "name": str(state.sku_ref.name or ""),
                "barcode": str(state.barcode or ""),
                "external_item_id": external_id,
                "fullbox_qty": fullbox_qty,
                "marketplace_qty": marketplace_qty,
                "difference": difference,
                "is_match": difference == 0,
                "mapping_missing": mapping_missing,
                "export_status": state.get_status_display(),
                "export_error": str(state.error or ""),
            }
        )

    for local_key, fullbox_qty in available.items():
        local_key = str(local_key)
        if local_key in used_keys or int(fullbox_qty or 0) <= 0:
            continue
        item = local_metadata.get(local_key, {})
        rows.append(
            {
                "profile_id": profile.id,
                "profile_name": profile.name,
                "marketplace": profile.marketplace,
                "marketplace_label": profile.get_marketplace_display(),
                "warehouse_note": warehouse_note,
                "sku_code": item.get("sku_code", ""),
                "name": item.get("name", ""),
                "barcode": item.get("barcode", ""),
                "external_item_id": "",
                "fullbox_qty": max(int(fullbox_qty or 0), 0),
                "marketplace_qty": None,
                "difference": None,
                "is_match": False,
                "mapping_missing": True,
                "export_status": "Нет сопоставления",
                "export_error": "Товар не сопоставлен с карточкой маркетплейса.",
            }
        )

    rows.sort(
        key=lambda row: (
            row["is_match"],
            not row["mapping_missing"],
            str(row["sku_code"] or "").casefold(),
            str(row["barcode"] or "").casefold(),
        )
    )
    summary = {
        "profile_id": profile.id,
        "profile_name": profile.name,
        "marketplace": profile.marketplace,
        "marketplace_label": profile.get_marketplace_display(),
        "warehouse_note": warehouse_note,
        "fullbox_total": sum(
            max(int(available.get(key, 0) or 0), 0)
            for key in mapped_keys
        ),
        "unmapped_fullbox_total": sum(
            max(int(value or 0), 0)
            for key, value in available.items()
            if str(key) not in mapped_keys
        ),
        "marketplace_total": sum(
            int(row["marketplace_qty"] or 0)
            for row in rows
            if row["marketplace_qty"] is not None
        ),
        "row_count": len(rows),
        "mismatch_count": sum(1 for row in rows if not row["is_match"]),
        "mapping_missing_count": sum(1 for row in rows if row["mapping_missing"]),
        "error": "",
    }
    return rows, summary


def build_agency_stock_comparison(
    *,
    agency_id: int,
    transport: MarketplaceReadTransport | None = None,
) -> dict:
    owned_transport = transport is None
    active_transport = transport or RequestsMarketplaceReadTransport(reuse_connections=True)
    profiles = list(
        FbsIntegrationProfile.objects.filter(
            agency_id=int(agency_id),
            is_active=True,
            stock_push_enabled=True,
            stock_mode=FbsIntegrationProfile.STOCK_MODE_MANAGED,
        )
        .select_related("agency")
        .order_by("marketplace", "name", "id")
    )
    result = {
        "checked_at": timezone.localtime().strftime("%d.%m.%Y %H:%M:%S"),
        "profiles": [],
        "rows": [],
        "has_profiles": bool(profiles),
    }
    try:
        for profile in profiles:
            try:
                rows, summary = _profile_rows(profile, transport=active_transport)
            except (FbsIntegrationError, TypeError, ValueError) as exc:
                result["profiles"].append(
                    {
                        "profile_id": profile.id,
                        "profile_name": profile.name,
                        "marketplace": profile.marketplace,
                        "marketplace_label": profile.get_marketplace_display(),
                        "warehouse_note": (
                            f"склад продавца {profile.external_warehouse_id}"
                            if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
                            else "все FBS-склады кабинета Ozon"
                        ),
                        "fullbox_total": None,
                        "unmapped_fullbox_total": None,
                        "marketplace_total": None,
                        "row_count": 0,
                        "mismatch_count": 0,
                        "mapping_missing_count": 0,
                        "error": str(exc),
                    }
                )
                continue
            result["profiles"].append(summary)
            result["rows"].extend(rows)
    finally:
        if owned_transport and hasattr(active_transport, "close"):
            active_transport.close()
    return result
