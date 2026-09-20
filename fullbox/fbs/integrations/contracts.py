from __future__ import annotations

from dataclasses import dataclass


WB_FETCH_ORDER_STICKER = "wb_fetch_order_sticker"
WB_SET_ORDER_SGTINS = "wb_set_order_sgtins"
WB_SET_ORDER_EXPIRATION = "wb_set_order_expiration"
WB_READ_ORDER_METADATA = "wb_read_order_metadata"
WB_CREATE_HANDOVER_SUPPLY = "wb_create_handover_supply"
WB_READ_HANDOVER_SUPPLIES = "wb_read_handover_supplies"
WB_ADD_ORDER_TO_HANDOVER = "wb_add_order_to_handover"
WB_CANCEL_ORDER = "wb_cancel_order"
WB_READ_HANDOVER_ORDER_IDS = "wb_read_handover_order_ids"
WB_READ_HANDOVER_BOXES = "wb_read_handover_boxes"
WB_CREATE_HANDOVER_BOXES = "wb_create_handover_boxes"
WB_FETCH_HANDOVER_BOX_LABEL = "wb_fetch_handover_box_label"
WB_DELIVER_HANDOVER = "wb_deliver_handover"
WB_READ_HANDOVER_SUPPLY = "wb_read_handover_supply"
WB_FETCH_HANDOVER_SUPPLY_LABEL = "wb_fetch_handover_supply_label"
OZON_SHIP_POSTING = "ozon_ship_posting"
OZON_READ_POSTING_AFTER_SHIP = "ozon_read_posting_after_ship"
OZON_FETCH_POSTING_LABEL = "ozon_fetch_posting_label"
OZON_CREATE_OR_GET_EXEMPLARS = "ozon_create_or_get_exemplars"
OZON_SET_EXEMPLARS = "ozon_set_exemplars"
OZON_READ_EXEMPLAR_STATUS = "ozon_read_exemplar_status"


@dataclass(frozen=True)
class MarketplaceCommandSpec:
    command_type: str
    http_method: str
    endpoint: str
    endpoint_version: str
    query: dict
    body: dict

    @property
    def payload(self) -> dict:
        return {"query": self.query, "body": self.body}


@dataclass(frozen=True)
class MarketplaceReadSpec:
    operation: str
    http_method: str
    endpoint: str
    endpoint_version: str
    query: dict
    body: dict


@dataclass(frozen=True)
class NormalizedMarketplaceItem:
    external_line_id: str
    external_sku: str
    product_name: str
    quantity: int
    binding_ids: tuple[str, ...]
    barcodes: tuple[str, ...]
    sku_codes: tuple[str, ...]
    requirements: dict
    raw_payload: dict


@dataclass(frozen=True)
class NormalizedMarketplaceOrder:
    external_order_id: str
    marketplace_status: str
    marketplace_substatus: str
    ordered_at: str
    cutoff_at: str
    warehouse_id: str
    items: tuple[NormalizedMarketplaceItem, ...]
    raw_payload: dict


@dataclass(frozen=True)
class ParsedMarketplaceLabel:
    external_label_id: str
    barcode: str
    label_format: str
    file_name: str
    content: bytes
    metadata: dict
