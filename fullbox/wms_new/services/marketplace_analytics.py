from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

import requests
from django.db import transaction
from django.utils import timezone

from sku.models import MarketCredential

from ..models import (
    WmsNewEvent,
    WmsNewMarketplaceStock,
    WmsNewOrderItem,
    WmsNewProduct,
)


class MarketplaceStockSyncError(ValueError):
    pass


def _positive_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _marketplace_code(credential: MarketCredential) -> str:
    name = str(credential.market.name or "").upper()
    if "OZON" in name or "ОЗОН" in name:
        return WmsNewMarketplaceStock.MARKETPLACE_OZON
    if "WB" in name or "WILDBERRIES" in name or "ВАЙЛДБЕРРИЗ" in name:
        return WmsNewMarketplaceStock.MARKETPLACE_WB
    return ""


def _wb_rows(credential: MarketCredential) -> list[dict]:
    token = str(credential.market_key or "").strip()
    response = requests.get(
        "https://statistics-api.wildberries.ru/api/v1/supplier/stocks",
        headers={"Authorization": token},
        params={"dateFrom": "2019-01-01"},
        timeout=45,
    )
    if response.status_code != 200:
        raise MarketplaceStockSyncError(f"Wildberries API: HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise MarketplaceStockSyncError("Wildberries API вернул некорректный JSON.") from exc
    if not isinstance(payload, list):
        raise MarketplaceStockSyncError("Wildberries API не вернул список остатков.")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        barcode = str(item.get("barcode") or item.get("Barcode") or "").strip()
        sku_code = str(
            item.get("supplierArticle")
            or item.get("vendorCode")
            or item.get("sa_name")
            or barcode
        ).strip()
        if not sku_code:
            continue
        warehouse_name = str(
            item.get("warehouseName") or item.get("warehouse") or "Wildberries"
        ).strip()
        rows.append(
            {
                "warehouse_code": warehouse_name,
                "warehouse_name": warehouse_name,
                "sku_code": sku_code,
                "barcode": barcode,
                "product_name": str(item.get("subject") or item.get("category") or sku_code),
                "quantity": _positive_int(item.get("quantityFull") or item.get("quantity")),
                "snapshot": {
                    "nm_id": item.get("nmId") or item.get("nm_id"),
                    "warehouse": warehouse_name,
                    "in_way_to_client": _positive_int(item.get("inWayToClient")),
                    "in_way_from_client": _positive_int(item.get("inWayFromClient")),
                },
            }
        )
    return rows


def _ozon_rows(credential: MarketCredential) -> list[dict]:
    token = str(credential.market_key or "").strip()
    client_id = str(credential.client_id or "").strip()
    if not client_id:
        raise MarketplaceStockSyncError("Ozon: не указан Client ID.")
    headers = {"Client-Id": client_id, "Api-Key": token, "Content-Type": "application/json"}
    cursor = ""
    rows = []
    for _ in range(40):
        body = {"filter": {"visibility": "ALL"}, "limit": 1000}
        if cursor:
            body["last_id"] = cursor
        response = requests.post(
            "https://api-seller.ozon.ru/v4/product/info/stocks",
            headers=headers,
            json=body,
            timeout=45,
        )
        if response.status_code != 200:
            raise MarketplaceStockSyncError(f"Ozon API: HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketplaceStockSyncError("Ozon API вернул некорректный JSON.") from exc
        result = payload.get("result") if isinstance(payload, dict) else None
        container = result if isinstance(result, dict) else payload
        items = container.get("items") if isinstance(container, dict) else None
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            sku_code = str(item.get("offer_id") or item.get("offerId") or "").strip()
            if not sku_code:
                continue
            stocks = item.get("stocks") if isinstance(item.get("stocks"), list) else []
            if not stocks:
                stocks = [{"present": item.get("present") or item.get("stock"), "type": "Ozon"}]
            for stock in stocks:
                if not isinstance(stock, dict):
                    continue
                warehouse_name = str(
                    stock.get("warehouse_name")
                    or stock.get("warehouseName")
                    or stock.get("type")
                    or "Ozon"
                ).strip()
                warehouse_code = str(
                    stock.get("warehouse_id")
                    or stock.get("warehouseId")
                    or warehouse_name
                ).strip()
                rows.append(
                    {
                        "warehouse_code": warehouse_code,
                        "warehouse_name": warehouse_name,
                        "sku_code": sku_code,
                        "barcode": "",
                        "product_name": sku_code,
                        "quantity": _positive_int(stock.get("present") or stock.get("quantity")),
                        "snapshot": {
                            "product_id": item.get("product_id") or item.get("productId"),
                            "reserved": _positive_int(stock.get("reserved")),
                            "type": stock.get("type"),
                        },
                    }
                )
        next_cursor = str(
            (container.get("cursor") or container.get("last_id") or "")
            if isinstance(container, dict)
            else ""
        )
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return rows


def _merge_rows(rows: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["warehouse_code"], row["sku_code"])
        if key not in merged:
            merged[key] = dict(row)
            continue
        merged[key]["quantity"] += row["quantity"]
    return list(merged.values())


def _product_maps(agency_id: int):
    by_article = {}
    by_barcode = {}
    for product in WmsNewProduct.objects.filter(agency_id=agency_id, is_archived=False):
        article = str(product.article or "").strip().lower()
        barcode = str(product.barcode or "").strip().lower()
        if article:
            by_article.setdefault(article, product)
        if barcode:
            by_barcode.setdefault(barcode, product)
    return by_article, by_barcode


def _sales_maps(agency_id: int, marketplace: str):
    since = timezone.now() - timedelta(days=30)
    items = WmsNewOrderItem.objects.filter(
        order__agency_id=agency_id,
        order__source_created_at__gte=since,
    )
    if marketplace == WmsNewMarketplaceStock.MARKETPLACE_OZON:
        items = items.filter(order__marketplace__icontains="ozon")
    else:
        items = items.filter(order__marketplace__iregex=r"wb|wildberries")
    by_article = defaultdict(int)
    by_barcode = defaultdict(int)
    for item in items.only("external_sku", "barcode", "quantity"):
        article = str(item.external_sku or "").strip().lower()
        barcode = str(item.barcode or "").strip().lower()
        if article:
            by_article[article] += item.quantity
        if barcode:
            by_barcode[barcode] += item.quantity
    return by_article, by_barcode


def _days_cover(quantity: int, sales_30d: int):
    if sales_30d > 0:
        return (Decimal(quantity * 30) / Decimal(sales_30d)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    if quantity > 0:
        return Decimal("999.00")
    return None


def refresh_marketplace_stocks(*, actor=None, agency_id: int | None = None) -> dict:
    credentials = MarketCredential.objects.exclude(market_key__isnull=True).exclude(
        market_key=""
    ).select_related("agency", "market")
    if agency_id:
        credentials = credentials.filter(agency_id=agency_id)
    credentials = [item for item in credentials if _marketplace_code(item)]
    if not credentials:
        raise MarketplaceStockSyncError("Нет настроенных интеграций маркетплейсов.")

    imported = updated = deleted = 0
    errors = []
    fetched_at = timezone.now()
    for credential in credentials:
        marketplace = _marketplace_code(credential)
        try:
            raw_rows = _wb_rows(credential) if marketplace == "wb" else _ozon_rows(credential)
            rows = _merge_rows(raw_rows)
        except (requests.RequestException, MarketplaceStockSyncError) as exc:
            errors.append(f"{credential.agency} / {credential.market}: {exc}")
            continue

        products_by_article, products_by_barcode = _product_maps(credential.agency_id)
        sales_by_article, sales_by_barcode = _sales_maps(credential.agency_id, marketplace)
        retained_ids = []
        with transaction.atomic():
            for row in rows:
                article_key = row["sku_code"].lower()
                barcode_key = row["barcode"].lower()
                product = products_by_article.get(article_key) or products_by_barcode.get(barcode_key)
                sales = sales_by_article.get(article_key, 0) or sales_by_barcode.get(barcode_key, 0)
                defaults = {
                    "product": product,
                    "credential_source_id": credential.id,
                    "warehouse_name": row["warehouse_name"],
                    "barcode": row["barcode"],
                    "product_name": product.name if product else row["product_name"],
                    "category": product.category if product else "",
                    "quantity": row["quantity"],
                    "fullbox_qty": product.stock_on_hand if product else 0,
                    "sales_30d": sales,
                    "days_cover": _days_cover(row["quantity"], sales),
                    "fetched_at": fetched_at,
                    "source_snapshot": row["snapshot"],
                }
                item, created = WmsNewMarketplaceStock.objects.update_or_create(
                    agency_id=credential.agency_id,
                    marketplace=marketplace,
                    warehouse_code=row["warehouse_code"],
                    sku_code=row["sku_code"],
                    defaults=defaults,
                )
                retained_ids.append(item.id)
                imported += int(created)
                updated += int(not created)
            stale = WmsNewMarketplaceStock.objects.filter(
                agency_id=credential.agency_id,
                marketplace=marketplace,
            )
            if retained_ids:
                stale = stale.exclude(id__in=retained_ids)
            stale_count, _ = stale.delete()
            deleted += stale_count
            WmsNewEvent.objects.create(
                entity_type="marketplace_stock",
                entity_id=credential.agency_id,
                action="refresh",
                actor=actor,
                after={
                    "marketplace": marketplace,
                    "rows": len(retained_ids),
                    "fetched_at": fetched_at.isoformat(),
                },
            )
    if errors and not imported and not updated:
        raise MarketplaceStockSyncError("; ".join(errors[:3]))
    return {
        "imported": imported,
        "updated": updated,
        "deleted": deleted,
        "errors": errors,
        "fetched_at": fetched_at,
    }
