from __future__ import annotations

import base64
import binascii
from dataclasses import replace
from datetime import date, timedelta

from fbs.exceptions import FbsIntegrationError
from fbs.models import FbsMarketplaceCommand, FbsOrder, FbsOrderLabel

from .contracts import (
    MarketplaceCommandSpec,
    MarketplaceReadSpec,
    NormalizedMarketplaceItem,
    NormalizedMarketplaceOrder,
    ParsedMarketplaceLabel,
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
from .http import MarketplaceHttpResponse


WB_STICKER_READY_STATUSES = {"confirm", "complete"}
WB_METADATA_WRITE_STATUS = "confirm"


def build_wb_create_handover_supply_spec(name: str) -> MarketplaceCommandSpec:
    name = str(name or "").strip()
    if not name or len(name) > 128:
        raise FbsIntegrationError("Название поставки WB должно содержать от 1 до 128 символов.")
    return MarketplaceCommandSpec(
        command_type=WB_CREATE_HANDOVER_SUPPLY,
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/api/v3/supplies",
        endpoint_version="v3",
        query={},
        body={"name": name},
    )


def build_wb_read_handover_supplies_spec() -> MarketplaceCommandSpec:
    return MarketplaceCommandSpec(
        command_type=WB_READ_HANDOVER_SUPPLIES,
        http_method=FbsMarketplaceCommand.METHOD_GET,
        endpoint="/api/v3/supplies",
        endpoint_version="v3",
        query={"limit": 1000, "next": 0},
        body={},
    )


def build_wb_add_order_to_handover_spec(
    *, supply_id: str, order_id: str
) -> MarketplaceCommandSpec:
    supply_id = str(supply_id or "").strip()
    try:
        external_order_id = int(order_id)
    except (TypeError, ValueError) as exc:
        raise FbsIntegrationError("ID сборочного задания WB должен быть числом.") from exc
    if not supply_id:
        raise FbsIntegrationError("WB еще не вернул ID поставки.")
    return MarketplaceCommandSpec(
        command_type=WB_ADD_ORDER_TO_HANDOVER,
        http_method=FbsMarketplaceCommand.METHOD_PATCH,
        endpoint=f"/api/marketplace/v3/supplies/{supply_id}/orders",
        endpoint_version="v3",
        query={},
        body={"orders": [external_order_id]},
    )


def build_wb_cancel_order_spec(order_id: str) -> MarketplaceCommandSpec:
    try:
        external_order_id = int(str(order_id or "").strip())
    except (TypeError, ValueError) as exc:
        raise FbsIntegrationError("WB не вернул корректный ID сборочного задания.") from exc
    if external_order_id <= 0:
        raise FbsIntegrationError("WB не вернул корректный ID сборочного задания.")
    return MarketplaceCommandSpec(
        command_type=WB_CANCEL_ORDER,
        http_method=FbsMarketplaceCommand.METHOD_PATCH,
        endpoint=f"/api/v3/orders/{external_order_id}/cancel",
        endpoint_version="v3",
        query={},
        body={},
    )


def build_wb_read_handover_order_ids_spec(supply_id: str) -> MarketplaceCommandSpec:
    supply_id = str(supply_id or "").strip()
    if not supply_id:
        raise FbsIntegrationError("WB еще не вернул ID поставки.")
    return MarketplaceCommandSpec(
        command_type=WB_READ_HANDOVER_ORDER_IDS,
        http_method=FbsMarketplaceCommand.METHOD_GET,
        endpoint=f"/api/marketplace/v3/supplies/{supply_id}/order-ids",
        endpoint_version="v3",
        query={},
        body={},
    )


def build_wb_read_handover_boxes_spec(supply_id: str) -> MarketplaceCommandSpec:
    supply_id = str(supply_id or "").strip()
    if not supply_id:
        raise FbsIntegrationError("WB еще не вернул ID поставки.")
    return MarketplaceCommandSpec(
        command_type=WB_READ_HANDOVER_BOXES,
        http_method=FbsMarketplaceCommand.METHOD_GET,
        endpoint=f"/api/v3/supplies/{supply_id}/trbx",
        endpoint_version="v3",
        query={},
        body={},
    )


def build_wb_create_handover_boxes_spec(
    *, supply_id: str, amount: int
) -> MarketplaceCommandSpec:
    supply_id = str(supply_id or "").strip()
    if not supply_id:
        raise FbsIntegrationError("WB еще не вернул ID поставки.")
    if not 1 <= int(amount) <= 1000:
        raise FbsIntegrationError("WB принимает от 1 до 1000 транспортных коробов.")
    return MarketplaceCommandSpec(
        command_type=WB_CREATE_HANDOVER_BOXES,
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint=f"/api/v3/supplies/{supply_id}/trbx",
        endpoint_version="v3",
        query={},
        body={"amount": int(amount)},
    )


def build_wb_handover_box_label_spec(
    *, supply_id: str, external_box_id: str
) -> MarketplaceCommandSpec:
    supply_id = str(supply_id or "").strip()
    external_box_id = str(external_box_id or "").strip()
    if not supply_id:
        raise FbsIntegrationError("WB еще не вернул ID поставки.")
    if not external_box_id:
        raise FbsIntegrationError("Не указан ID транспортного короба WB.")
    return MarketplaceCommandSpec(
        command_type=WB_FETCH_HANDOVER_BOX_LABEL,
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint=f"/api/v3/supplies/{supply_id}/trbx/stickers",
        endpoint_version="v3",
        query={"type": "png"},
        body={"trbxIds": [external_box_id]},
    )


def build_wb_deliver_handover_spec(supply_id: str) -> MarketplaceCommandSpec:
    supply_id = str(supply_id or "").strip()
    if not supply_id:
        raise FbsIntegrationError("WB еще не вернул ID поставки.")
    return MarketplaceCommandSpec(
        command_type=WB_DELIVER_HANDOVER,
        http_method=FbsMarketplaceCommand.METHOD_PATCH,
        endpoint=f"/api/v3/supplies/{supply_id}/deliver",
        endpoint_version="v3",
        query={},
        body={},
    )


def build_wb_read_handover_supply_spec(supply_id: str) -> MarketplaceCommandSpec:
    supply_id = str(supply_id or "").strip()
    if not supply_id:
        raise FbsIntegrationError("WB еще не вернул ID поставки.")
    return MarketplaceCommandSpec(
        command_type=WB_READ_HANDOVER_SUPPLY,
        http_method=FbsMarketplaceCommand.METHOD_GET,
        endpoint=f"/api/v3/supplies/{supply_id}",
        endpoint_version="v3",
        query={},
        body={},
    )


def build_wb_handover_supply_label_spec(supply_id: str) -> MarketplaceCommandSpec:
    supply_id = str(supply_id or "").strip()
    if not supply_id:
        raise FbsIntegrationError("WB еще не вернул ID поставки.")
    return MarketplaceCommandSpec(
        command_type=WB_FETCH_HANDOVER_SUPPLY_LABEL,
        http_method=FbsMarketplaceCommand.METHOD_GET,
        endpoint=f"/api/v3/supplies/{supply_id}/barcode",
        endpoint_version="v3",
        query={"type": "png"},
        body={},
    )


def parse_wb_supply_id(payload: dict | list | None) -> str:
    supply_id = str(payload.get("id") or "").strip() if isinstance(payload, dict) else ""
    if not supply_id.startswith("WB-GI-"):
        raise FbsIntegrationError("WB не вернул корректный ID поставки.")
    return supply_id


def parse_wb_supply_order_ids(payload: dict | list | None) -> tuple[str, ...]:
    values = payload.get("orderIds") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise FbsIntegrationError("WB не вернул состав поставки.")
    return tuple(str(value) for value in values)


def parse_wb_handover_box_ids(payload: dict | list | None) -> tuple[str, ...]:
    rows = payload.get("trbxes") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise FbsIntegrationError("WB не вернул список транспортных коробов.")
    return tuple(
        str(row.get("id") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    )


def parse_wb_created_handover_box_ids(payload: dict | list | None) -> tuple[str, ...]:
    values = payload.get("trbxIds") if isinstance(payload, dict) else None
    if not isinstance(values, list) or not values:
        raise FbsIntegrationError("WB не вернул ID созданных транспортных коробов.")
    return tuple(str(value or "").strip() for value in values if str(value or "").strip())


def parse_wb_png_label(payload: dict | list | None, *, source: str) -> tuple[str, bytes]:
    raw = payload
    if source == "box":
        stickers = payload.get("stickers") if isinstance(payload, dict) else None
        if not isinstance(stickers, list) or len(stickers) != 1 or not isinstance(stickers[0], dict):
            raise FbsIntegrationError("WB не вернул единственный QR транспортного короба.")
        raw = stickers[0]
    if not isinstance(raw, dict):
        raise FbsIntegrationError("WB вернул QR в неверном формате.")
    barcode = str(raw.get("barcode") or "").strip()
    encoded_file = str(raw.get("file") or "").strip()
    try:
        content = base64.b64decode(encoded_file, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise FbsIntegrationError("WB вернул поврежденный файл QR.") from exc
    if not barcode or not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise FbsIntegrationError("WB не вернул корректный PNG QR.")
    return barcode, content


def build_wb_new_orders_spec() -> MarketplaceReadSpec:
    return MarketplaceReadSpec(
        operation="wb_pull_new_orders",
        http_method=FbsMarketplaceCommand.METHOD_GET,
        endpoint="/api/v3/orders/new",
        endpoint_version="v3",
        query={},
        body={},
    )


def build_wb_statuses_spec(order_ids: list[int]) -> MarketplaceReadSpec:
    if not order_ids or len(order_ids) > 1000:
        raise FbsIntegrationError("WB принимает от 1 до 1000 ID для сверки статусов.")
    return MarketplaceReadSpec(
        operation="wb_pull_order_statuses",
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/api/v3/orders/status",
        endpoint_version="v3",
        query={},
        body={"orders": order_ids},
    )


def build_wb_cards_spec(*, text_search: str, limit: int = 100) -> MarketplaceReadSpec:
    text_search = str(text_search or "").strip()
    if not text_search:
        raise FbsIntegrationError("Для поиска карточки WB нужен артикул клиента.")
    return MarketplaceReadSpec(
        operation="wb_read_product_cards",
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/content/v2/get/cards/list",
        endpoint_version="v2",
        query={},
        body={
            "settings": {
                "sort": {"ascending": True},
                "filter": {"withPhoto": -1, "textSearch": text_search},
                "cursor": {"limit": min(max(int(limit), 1), 100)},
            }
        },
    )


def parse_wb_card_stock_bindings(
    payload: dict | list | None,
    *,
    barcodes: set[str],
) -> dict[str, tuple[str, ...]]:
    cards = payload.get("cards") if isinstance(payload, dict) else None
    if not isinstance(cards, list):
        raise FbsIntegrationError("WB не вернул список карточек товаров.")
    targets = {str(value or "").strip() for value in barcodes if str(value or "").strip()}
    found: dict[str, set[str]] = {barcode: set() for barcode in targets}
    for card in cards:
        if not isinstance(card, dict):
            continue
        sizes = card.get("sizes") if isinstance(card.get("sizes"), list) else []
        for size in sizes:
            if not isinstance(size, dict):
                continue
            chrt_id = str(size.get("chrtID") or size.get("chrtId") or "").strip()
            skus = size.get("skus") if isinstance(size.get("skus"), list) else []
            if not chrt_id.isdigit():
                continue
            for barcode in {str(value or "").strip() for value in skus} & targets:
                found[barcode].add(chrt_id)
    return {
        barcode: tuple(sorted(values, key=int))
        for barcode, values in found.items()
    }


def build_wb_stock_update_spec(
    *,
    warehouse_id: str,
    stocks: list[dict],
) -> MarketplaceReadSpec:
    warehouse_id = str(warehouse_id or "").strip()
    if not warehouse_id.isdigit():
        raise FbsIntegrationError("ID склада WB для выгрузки остатков должен быть числом.")
    if not stocks or len(stocks) > 1000:
        raise FbsIntegrationError("WB принимает от 1 до 1000 остатков за запрос.")
    prepared = []
    for row in stocks:
        chrt_id = str(row.get("chrtId") or "").strip()
        try:
            amount = int(row.get("amount"))
        except (TypeError, ValueError) as exc:
            raise FbsIntegrationError("Количество остатка WB должно быть целым числом.") from exc
        if not chrt_id.isdigit() or amount < 0:
            raise FbsIntegrationError("Остаток WB содержит неверный chrtId или количество.")
        prepared.append({"chrtId": int(chrt_id), "amount": amount})
    return MarketplaceReadSpec(
        operation="wb_update_fbs_stocks",
        http_method=FbsMarketplaceCommand.METHOD_PUT,
        endpoint=f"/api/v3/stocks/{warehouse_id}",
        endpoint_version="v3",
        query={},
        body={"stocks": prepared},
    )


def parse_wb_new_orders(payload: dict | list | None) -> tuple[NormalizedMarketplaceOrder, ...]:
    orders = payload.get("orders") if isinstance(payload, dict) else None
    if not isinstance(orders, list):
        raise FbsIntegrationError("WB не вернул список новых сборочных заданий.")
    normalized = []
    for raw in orders:
        if not isinstance(raw, dict):
            raise FbsIntegrationError("WB вернул сборочное задание в неверном формате.")
        external_order_id = str(raw.get("id") or "").strip()
        if not external_order_id:
            raise FbsIntegrationError("WB вернул сборочное задание без ID.")
        nm_id = str(raw.get("nmId") or "").strip()
        chrt_id = str(raw.get("chrtId") or "").strip()
        raw_skus = raw.get("skus") if isinstance(raw.get("skus"), list) else []
        options = raw.get("options") if isinstance(raw.get("options"), dict) else {}
        barcodes = tuple(
            value for value in (str(item).strip() for item in raw_skus) if value
        )
        required_meta = raw.get("requiredMeta") if isinstance(raw.get("requiredMeta"), list) else []
        optional_meta = raw.get("optionalMeta") if isinstance(raw.get("optionalMeta"), list) else []
        item = NormalizedMarketplaceItem(
            external_line_id=external_order_id,
            external_sku=nm_id or chrt_id or (barcodes[0] if barcodes else external_order_id),
            product_name=str(raw.get("article") or raw.get("subjectName") or "").strip(),
            quantity=1,
            binding_ids=tuple(value for value in (nm_id,) if value),
            barcodes=barcodes,
            sku_codes=tuple(
                value
                for value in (str(raw.get("article") or "").strip(),)
                if value
            ),
            requirements={
                "required_meta": required_meta,
                "optional_meta": optional_meta,
                "cargo_type": raw.get("cargoType"),
                "is_b2b": bool(
                    options["isB2B"]
                    if "isB2B" in options
                    else options.get("isB2b", False)
                ),
            },
            raw_payload=raw,
        )
        normalized.append(
            NormalizedMarketplaceOrder(
                external_order_id=external_order_id,
                marketplace_status="new",
                marketplace_substatus="",
                ordered_at=str(raw.get("createdAt") or "").strip(),
                cutoff_at=str(raw.get("sellerDate") or raw.get("supplyDate") or "").strip(),
                warehouse_id=str(raw.get("warehouseId") or "").strip(),
                items=(item,),
                raw_payload=raw,
            )
        )
    return tuple(normalized)


def build_wb_orders_metadata_spec(order_ids: list[int]) -> MarketplaceReadSpec:
    if not order_ids or len(order_ids) > 100:
        raise FbsIntegrationError(
            "WB принимает от 1 до 100 ID для чтения метаданных заказов."
        )
    if any(not isinstance(order_id, int) or order_id <= 0 for order_id in order_ids):
        raise FbsIntegrationError("ID сборочного задания WB должен быть положительным числом.")
    return MarketplaceReadSpec(
        operation="wb_pull_order_metadata",
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/api/marketplace/v3/orders/meta",
        endpoint_version="v3",
        query={},
        body={"orders": order_ids},
    )


def _normalized_wb_metadata_details(raw_order: dict) -> dict[str, dict]:
    raw_details = raw_order.get("metaDetails") or raw_order.get("meta_details")
    normalized: dict[str, dict] = {}
    if isinstance(raw_details, list):
        for detail in raw_details:
            if not isinstance(detail, dict):
                continue
            key = str(
                detail.get("key") or detail.get("type") or detail.get("name") or ""
            ).strip().casefold()
            if not key:
                continue
            normalized[key] = {
                "decision": str(detail.get("decision") or "").strip().casefold(),
                "value": detail.get("value"),
                "errors": detail.get("errors") or detail.get("error") or [],
            }
        return normalized

    # Compatibility with the legacy response during WB's meta -> metaDetails
    # transition.  A legacy value has already been accepted by WB.
    legacy_meta = raw_order.get("meta")
    if not isinstance(legacy_meta, dict):
        return normalized
    for raw_key, raw_value in legacy_meta.items():
        key = str(raw_key or "").strip().casefold()
        if not key:
            continue
        detail = raw_value if isinstance(raw_value, dict) else {"value": raw_value}
        value = detail.get("value")
        normalized[key] = {
            "decision": str(detail.get("decision") or "filled").strip().casefold()
            if value not in (None, "", [], {})
            else str(detail.get("decision") or "").strip().casefold(),
            "value": value,
            "errors": detail.get("errors") or detail.get("error") or [],
        }
    return normalized


def parse_wb_orders_metadata(
    payload: dict | list | None,
) -> dict[str, dict[str, dict]]:
    if isinstance(payload, dict):
        raw_orders = payload.get("orders")
        if raw_orders is None:
            raw_orders = payload.get("data")
        if raw_orders is None and any(
            key in payload for key in ("id", "orderId", "metaDetails", "meta")
        ):
            raw_orders = [payload]
    else:
        raw_orders = payload
    if not isinstance(raw_orders, list):
        raise FbsIntegrationError("WB не вернул метаданные сборочных заданий.")

    normalized: dict[str, dict[str, dict]] = {}
    for raw_order in raw_orders:
        if not isinstance(raw_order, dict):
            continue
        external_order_id = str(
            raw_order.get("id") or raw_order.get("orderId") or ""
        ).strip()
        if not external_order_id:
            continue
        normalized[external_order_id] = _normalized_wb_metadata_details(raw_order)
    return normalized


def _wb_metadata_values(value) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = (value,)
    return tuple(
        dict.fromkeys(
            str(raw or "").strip()
            for raw in raw_values
            if str(raw or "").strip()
        )
    )


def apply_wb_metadata_to_requirements(
    requirements: dict | None,
    metadata_details: dict | None,
) -> dict:
    prepared = dict(requirements or {})
    details = metadata_details if isinstance(metadata_details, dict) else {}
    sgtin = details.get("sgtin") if isinstance(details.get("sgtin"), dict) else None
    codes = _wb_metadata_values(sgtin.get("value")) if sgtin is not None else ()
    wb_meta = dict(prepared.get("wb_meta") or {})
    wb_meta["sgtin"] = {
        "available": sgtin is not None,
        "decision": str((sgtin or {}).get("decision") or "").strip().casefold(),
        "has_value": bool(codes),
        "has_errors": bool((sgtin or {}).get("errors")),
    }
    prepared["wb_meta"] = wb_meta
    prepared["wb_marking_codes"] = list(codes)
    return prepared


def enrich_wb_orders_with_metadata(
    orders: list[NormalizedMarketplaceOrder] | tuple[NormalizedMarketplaceOrder, ...],
    metadata_by_order: dict[str, dict[str, dict]],
) -> tuple[NormalizedMarketplaceOrder, ...]:
    enriched = []
    for order in orders:
        details = metadata_by_order.get(str(order.external_order_id))
        if details is None:
            enriched.append(order)
            continue
        items = tuple(
            replace(
                item,
                requirements=apply_wb_metadata_to_requirements(
                    item.requirements,
                    details,
                ),
            )
            for item in order.items
        )
        raw_payload = dict(order.raw_payload or {})
        # The metadata snapshot is part of the event identity so a changed WB
        # decision or code is ingested instead of being discarded as duplicate.
        raw_payload["_fullbox_wb_meta"] = details
        enriched.append(replace(order, items=items, raw_payload=raw_payload))
    return tuple(enriched)


def parse_wb_statuses(payload: dict | list | None) -> tuple[dict, ...]:
    orders = payload.get("orders") if isinstance(payload, dict) else None
    if not isinstance(orders, list):
        raise FbsIntegrationError("WB не вернул список статусов сборочных заданий.")
    normalized = []
    for raw in orders:
        if not isinstance(raw, dict):
            raise FbsIntegrationError("WB вернул статус сборочного задания в неверном формате.")
        external_order_id = str(raw.get("id") or "").strip()
        status = str(raw.get("supplierStatus") or "").strip().lower()
        if not external_order_id or not status:
            raise FbsIntegrationError("WB вернул статус без ID или supplierStatus.")
        normalized.append(
            {
                "external_order_id": external_order_id,
                "marketplace_status": status,
                "marketplace_substatus": str(raw.get("wbStatus") or "").strip().lower(),
                "raw_payload": raw,
            }
        )
    return tuple(normalized)


def build_wb_sticker_spec(order: FbsOrder) -> MarketplaceCommandSpec:
    if str(order.marketplace_status or "").strip().lower() not in WB_STICKER_READY_STATUSES:
        raise FbsIntegrationError("Заказ WB еще не включен в открытую поставку.")
    try:
        external_order_id = int(order.external_order_id)
    except (TypeError, ValueError) as exc:
        raise FbsIntegrationError("ID сборочного задания WB должен быть числом.") from exc
    return MarketplaceCommandSpec(
        command_type=WB_FETCH_ORDER_STICKER,
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/api/v3/orders/stickers",
        endpoint_version="v3",
        query={"type": "png", "width": 58, "height": 40},
        body={"orders": [external_order_id]},
    )


def _wb_order_id(order: FbsOrder) -> int:
    try:
        return int(order.external_order_id)
    except (TypeError, ValueError) as exc:
        raise FbsIntegrationError("ID сборочного задания WB должен быть числом.") from exc


def _require_wb_metadata_status(order: FbsOrder) -> None:
    if str(order.marketplace_status or "").strip().lower() != WB_METADATA_WRITE_STATUS:
        raise FbsIntegrationError(
            "Метаданные WB можно передать только для задания в статусе confirm."
        )


def build_wb_sgtin_spec(order: FbsOrder, sgtins: list[str]) -> MarketplaceCommandSpec:
    _require_wb_metadata_status(order)
    values = list(dict.fromkeys(str(value or "").strip() for value in sgtins))
    if not values or len(values) > 100 or any(not 16 <= len(value) <= 135 for value in values):
        raise FbsIntegrationError("WB принимает от 1 до 100 КИЗов длиной от 16 до 135 символов.")
    order_id = _wb_order_id(order)
    return MarketplaceCommandSpec(
        command_type=WB_SET_ORDER_SGTINS,
        http_method=FbsMarketplaceCommand.METHOD_PUT,
        endpoint=f"/api/v3/orders/{order_id}/meta/sgtin",
        endpoint_version="v3",
        query={},
        body={"sgtins": values},
    )


def build_wb_expiration_spec(order: FbsOrder, expiration: str) -> MarketplaceCommandSpec:
    _require_wb_metadata_status(order)
    try:
        parsed = date.fromisoformat(str(expiration or "").strip())
    except ValueError as exc:
        raise FbsIntegrationError("Срок годности WB должен быть корректной датой.") from exc
    if parsed < date.today() + timedelta(days=30):
        raise FbsIntegrationError("WB принимает срок годности не менее 30 дней от текущей даты.")
    order_id = _wb_order_id(order)
    return MarketplaceCommandSpec(
        command_type=WB_SET_ORDER_EXPIRATION,
        http_method=FbsMarketplaceCommand.METHOD_PUT,
        endpoint=f"/api/v3/orders/{order_id}/meta/expiration",
        endpoint_version="v3",
        query={},
        body={"expiration": parsed.strftime("%d.%m.%Y")},
    )


def build_wb_metadata_readback_spec(order: FbsOrder) -> MarketplaceCommandSpec:
    return MarketplaceCommandSpec(
        command_type=WB_READ_ORDER_METADATA,
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/api/marketplace/v3/orders/meta",
        endpoint_version="v3",
        query={},
        body={"orders": [_wb_order_id(order)]},
    )


def parse_wb_metadata_response(
    order: FbsOrder,
    response: MarketplaceHttpResponse,
) -> dict:
    payload = response.json_payload
    if isinstance(payload, dict):
        raw_orders = payload.get("orders") or payload.get("data")
        if raw_orders is None and any(key in payload for key in ("id", "orderId", "metaDetails")):
            raw_orders = [payload]
    else:
        raw_orders = payload
    if not isinstance(raw_orders, list):
        raise FbsIntegrationError("WB не вернул метаданные сборочных заданий.")
    expected_id = str(order.external_order_id)
    raw_order = next(
        (
            value
            for value in raw_orders
            if isinstance(value, dict)
            and str(value.get("id") or value.get("orderId") or "") == expected_id
        ),
        None,
    )
    if raw_order is None:
        raise FbsIntegrationError("WB не вернул метаданные нужного сборочного задания.")
    normalized = _normalized_wb_metadata_details(raw_order)
    if not normalized:
        raise FbsIntegrationError("WB вернул пустой массив metaDetails.")
    return {"order_id": expected_id, "meta_details": normalized}


def parse_wb_sticker_response(
    order: FbsOrder,
    response: MarketplaceHttpResponse,
) -> ParsedMarketplaceLabel:
    payload = response.json_payload
    stickers = payload.get("stickers") if isinstance(payload, dict) else None
    if not isinstance(stickers, list):
        raise FbsIntegrationError("WB не вернул список стикеров.")
    expected_id = str(order.external_order_id)
    sticker = next(
        (
            value
            for value in stickers
            if isinstance(value, dict) and str(value.get("orderId")) == expected_id
        ),
        None,
    )
    if sticker is None:
        raise FbsIntegrationError("WB не вернул стикер нужного заказа.")
    barcode = str(sticker.get("barcode") or "").strip()
    encoded_file = str(sticker.get("file") or "").strip()
    if not barcode or not encoded_file:
        raise FbsIntegrationError("Ответ WB не содержит штрихкод или файл стикера.")
    try:
        content = base64.b64decode(encoded_file, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise FbsIntegrationError("WB вернул поврежденный файл стикера.") from exc
    if not content or len(content) > 10 * 1024 * 1024:
        raise FbsIntegrationError("Размер файла стикера WB недопустим.")
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise FbsIntegrationError("WB вернул файл стикера не в формате PNG.")
    part_a = str(sticker.get("partA") or "").strip()
    part_b = str(sticker.get("partB") or "").strip()
    external_label_id = "-".join(value for value in (part_a, part_b) if value) or barcode
    return ParsedMarketplaceLabel(
        external_label_id=external_label_id,
        barcode=barcode,
        label_format=FbsOrderLabel.FORMAT_PNG,
        file_name=f"wb-{expected_id}.png",
        content=content,
        metadata={"order_id": expected_id, "part_a": part_a, "part_b": part_b},
    )
