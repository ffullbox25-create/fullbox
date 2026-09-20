from __future__ import annotations

from datetime import date

from fbs.exceptions import FbsIntegrationError
from fbs.models import FbsMarketplaceCommand, FbsOrder, FbsOrderLabel

from .contracts import (
    MarketplaceCommandSpec,
    MarketplaceReadSpec,
    NormalizedMarketplaceItem,
    NormalizedMarketplaceOrder,
    OZON_CREATE_OR_GET_EXEMPLARS,
    OZON_FETCH_POSTING_LABEL,
    OZON_READ_EXEMPLAR_STATUS,
    OZON_READ_POSTING_AFTER_SHIP,
    OZON_SET_EXEMPLARS,
    OZON_SHIP_POSTING,
    ParsedMarketplaceLabel,
)
from .http import MarketplaceHttpResponse


OZON_AWAITING_PACKAGING = "awaiting_packaging"
OZON_AWAITING_DELIVER = "awaiting_deliver"
OZON_HANDOVER_DOCUMENT_BARCODE = "barcode"
OZON_HANDOVER_DOCUMENT_PDF = "pdf"


def build_ozon_postings_spec(
    *,
    since: str,
    to: str,
    cursor: str = "",
    limit: int = 100,
) -> MarketplaceReadSpec:
    if not since or not to:
        raise FbsIntegrationError("Для чтения отправлений Ozon нужен временной интервал.")
    return MarketplaceReadSpec(
        operation="ozon_pull_fbs_postings",
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/v4/posting/fbs/list",
        endpoint_version="v4",
        query={},
        body={
            "sort_dir": "asc",
            "filter": {"since": since, "to": to},
            "limit": max(1, min(int(limit), 1000)),
            "cursor": str(cursor or "").strip(),
            "with": {
                "analytics_data": False,
                "barcodes": True,
                "financial_data": False,
                "translit": False,
            },
        },
    )


def build_ozon_posting_status_spec(posting_number: str) -> MarketplaceReadSpec:
    posting_number = str(posting_number or "").strip()
    if not posting_number:
        raise FbsIntegrationError("Для сверки Ozon нужен номер отправления.")
    return MarketplaceReadSpec(
        operation="ozon_pull_posting_status",
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/v3/posting/fbs/get",
        endpoint_version="v3",
        query={},
        body={"posting_number": posting_number, "with": {"barcodes": True}},
    )


def build_ozon_carriage_list_spec(*, departure_date: str) -> MarketplaceReadSpec:
    """Build the read-only request used to find an already-created carriage."""
    departure_date = str(departure_date or "").strip()
    try:
        date.fromisoformat(departure_date)
    except ValueError as exc:
        raise FbsIntegrationError("Для поиска отгрузки Ozon нужна дата в формате ГГГГ-ММ-ДД.") from exc
    return MarketplaceReadSpec(
        operation="ozon_read_fbs_carriages",
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/v2/carriage/delivery/list",
        endpoint_version="v2",
        query={},
        body={
            "cursor": "",
            "filter": {"departure_date": departure_date},
            "limit": 1000,
        },
    )


def parse_ozon_carriage_list(payload: dict | list | None) -> tuple[dict, ...]:
    methods = payload.get("methods") if isinstance(payload, dict) else None
    if methods is None and isinstance(payload, dict):
        methods = payload.get("result")
    if not isinstance(methods, list):
        raise FbsIntegrationError("Ozon не вернул список перевозок.")
    result = []
    for method in methods:
        if not isinstance(method, dict):
            continue
        carriages = method.get("carriages")
        if not isinstance(carriages, list):
            carriages = []
        result.append(
            {
                "delivery_method_id": str(method.get("delivery_method_id") or "").strip(),
                "departure_date": str(method.get("departure_date") or "").strip(),
                "carriages": tuple(
                    carriage for carriage in carriages if isinstance(carriage, dict)
                ),
            }
        )
    return tuple(result)


def build_ozon_carriage_postings_spec(carriage_id: int) -> MarketplaceReadSpec:
    try:
        carriage_id = int(carriage_id)
    except (TypeError, ValueError) as exc:
        raise FbsIntegrationError("Номер перевозки Ozon должен быть числом.") from exc
    if carriage_id <= 0:
        raise FbsIntegrationError("Номер перевозки Ozon должен быть положительным.")
    return MarketplaceReadSpec(
        operation="ozon_read_fbs_carriage_postings",
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/v2/posting/fbs/act/get-postings",
        endpoint_version="v2",
        query={},
        body={"id": carriage_id},
    )


def parse_ozon_carriage_postings(payload: dict | list | None) -> tuple[str, ...]:
    postings = payload.get("result") if isinstance(payload, dict) else payload
    if isinstance(postings, dict):
        postings = postings.get("postings")
    if not isinstance(postings, list):
        raise FbsIntegrationError("Ozon не вернул состав перевозки.")
    numbers = []
    for posting in postings:
        number = (
            str(posting.get("posting_number") or "").strip()
            if isinstance(posting, dict)
            else str(posting or "").strip()
        )
        if not number:
            raise FbsIntegrationError("Ozon вернул перевозку с отправлением без номера.")
        numbers.append(number)
    return tuple(numbers)


def build_ozon_handover_document_spec(
    *, carriage_id: int, document_kind: str
) -> MarketplaceReadSpec:
    try:
        carriage_id = int(carriage_id)
    except (TypeError, ValueError) as exc:
        raise FbsIntegrationError("Номер перевозки Ozon должен быть числом.") from exc
    if carriage_id <= 0:
        raise FbsIntegrationError("Номер перевозки Ozon должен быть положительным.")
    endpoints = {
        OZON_HANDOVER_DOCUMENT_BARCODE: "/v2/posting/fbs/act/get-barcode",
        OZON_HANDOVER_DOCUMENT_PDF: "/v2/posting/fbs/act/get-pdf",
    }
    endpoint = endpoints.get(str(document_kind or "").strip())
    if endpoint is None:
        raise FbsIntegrationError("Неизвестный тип документа Ozon.")
    return MarketplaceReadSpec(
        operation=f"ozon_read_fbs_carriage_{document_kind}",
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint=endpoint,
        endpoint_version="v2",
        query={},
        body={"id": carriage_id},
    )


def parse_ozon_handover_document_response(
    response: MarketplaceHttpResponse, *, document_kind: str
) -> tuple[bytes, str]:
    content = bytes(response.content or b"")
    if not 200 <= int(response.status_code or 0) < 300:
        raise FbsIntegrationError(
            f"Ozon не выдал документ перевозки (HTTP {response.status_code})."
        )
    if not content or len(content) > 20 * 1024 * 1024:
        raise FbsIntegrationError("Размер документа отгрузки Ozon недопустим.")
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if document_kind == OZON_HANDOVER_DOCUMENT_BARCODE:
        if not content.startswith(b"\x89PNG\r\n\x1a\n") or "png" not in content_type:
            raise FbsIntegrationError("Ozon вернул штрихкод отгрузки не в формате PNG.")
        return content, "image/png"
    if document_kind == OZON_HANDOVER_DOCUMENT_PDF:
        if not content.startswith(b"%PDF-") or "pdf" not in content_type:
            raise FbsIntegrationError("Ozon вернул документы отгрузки не в формате PDF.")
        return content, "application/pdf"
    raise FbsIntegrationError("Неизвестный тип документа Ozon.")


def build_ozon_stock_update_spec(
    *,
    warehouse_id: str,
    stocks: list[dict],
) -> MarketplaceReadSpec:
    warehouse_id = str(warehouse_id or "").strip()
    if not warehouse_id.isdigit():
        raise FbsIntegrationError("ID склада Ozon для выгрузки остатков должен быть числом.")
    if not stocks or len(stocks) > 100:
        raise FbsIntegrationError("Ozon принимает от 1 до 100 остатков за запрос.")
    prepared = []
    for row in stocks:
        offer_id = str(row.get("offer_id") or "").strip()
        product_id = str(row.get("product_id") or "").strip()
        try:
            stock = int(row.get("stock"))
        except (TypeError, ValueError) as exc:
            raise FbsIntegrationError("Количество остатка Ozon должно быть целым числом.") from exc
        if not offer_id or stock < 0:
            raise FbsIntegrationError("Остаток Ozon содержит неверный offer_id или количество.")
        item = {
            "offer_id": offer_id,
            "stock": stock,
            "warehouse_id": int(warehouse_id),
        }
        if product_id:
            if not product_id.isdigit():
                raise FbsIntegrationError("product_id Ozon должен быть числом.")
            item["product_id"] = int(product_id)
        prepared.append(item)
    return MarketplaceReadSpec(
        operation="ozon_update_fbs_stocks",
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/v2/products/stocks",
        endpoint_version="v2",
        query={},
        body={"stocks": prepared},
    )


def parse_ozon_posting_status(payload: dict | list | None) -> dict:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise FbsIntegrationError("Ozon не вернул данные FBS-отправления.")
    posting_number = str(result.get("posting_number") or "").strip()
    status = str(result.get("status") or "").strip().lower()
    if not posting_number or not status:
        raise FbsIntegrationError("Ozon вернул отправление без номера или статуса.")
    return {
        "external_order_id": posting_number,
        "marketplace_status": status,
        "marketplace_substatus": str(result.get("substatus") or "").strip().lower(),
        "raw_payload": result,
    }


def parse_ozon_postings(
    payload: dict | list | None,
) -> tuple[tuple[NormalizedMarketplaceOrder, ...], bool, str]:
    result = payload.get("result") if isinstance(payload, dict) else None
    container = result if isinstance(result, dict) else payload
    postings = container.get("postings") if isinstance(container, dict) else None
    if not isinstance(postings, list):
        raise FbsIntegrationError("Ozon не вернул список FBS-отправлений.")
    normalized = []
    for raw in postings:
        if not isinstance(raw, dict):
            raise FbsIntegrationError("Ozon вернул отправление в неверном формате.")
        posting_number = str(raw.get("posting_number") or "").strip()
        if not posting_number:
            raise FbsIntegrationError("Ozon вернул отправление без posting_number.")
        products = raw.get("products")
        if not isinstance(products, list) or not products:
            raise FbsIntegrationError("Ozon вернул FBS-отправление без товаров.")
        items = []
        line_occurrences = {}
        for product in products:
            if not isinstance(product, dict):
                raise FbsIntegrationError("Ozon вернул товар отправления в неверном формате.")
            ozon_sku = str(product.get("sku") or "").strip()
            marketplace_sku = str(
                ozon_sku or product.get("product_id") or ""
            ).strip()
            product_id = str(product.get("product_id") or ozon_sku or "").strip()
            offer_id = str(
                product.get("offer_id")
                or product.get("offerId")
                or product.get("product_offer_id")
                or ""
            ).strip()
            line_key = marketplace_sku or offer_id
            if not line_key:
                raise FbsIntegrationError("Ozon вернул товар без sku и offer_id.")
            try:
                quantity = int(product.get("quantity") or 0)
            except (TypeError, ValueError) as exc:
                raise FbsIntegrationError("Ozon вернул неверное количество товара.") from exc
            if quantity <= 0:
                raise FbsIntegrationError("Ozon вернул неположительное количество товара.")
            occurrence = line_occurrences.get(line_key, 0) + 1
            line_occurrences[line_key] = occurrence
            external_line_id = line_key if occurrence == 1 else f"{line_key}:{occurrence}"
            raw_barcodes = (
                product.get("barcodes") if isinstance(product.get("barcodes"), list) else []
            )
            raw_mandatory_mark = product.get("mandatory_mark")
            if isinstance(raw_mandatory_mark, list):
                mandatory_mark = raw_mandatory_mark
            elif raw_mandatory_mark in (None, ""):
                mandatory_mark = []
            else:
                mandatory_mark = [raw_mandatory_mark]
            posting_requirements = (
                raw.get("requirements") if isinstance(raw.get("requirements"), dict) else {}
            )
            required_product_ids = {
                str(value).strip()
                for value in posting_requirements.get(
                    "products_requiring_mandatory_mark", []
                )
                if str(value).strip()
            }
            is_kiz = bool(product.get("is_kiz")) or product_id in required_product_ids
            if is_kiz and not mandatory_mark:
                mandatory_mark = ["mandatory_mark"]
            items.append(
                NormalizedMarketplaceItem(
                    external_line_id=external_line_id,
                    external_sku=offer_id or marketplace_sku,
                    product_name=str(
                        product.get("name") or product.get("product_name") or ""
                    ).strip(),
                    quantity=quantity,
                    # Ozon FBS postings identify the product with `sku`.  This
                    # is different from the catalog `product_id` and is the
                    # stable key stored in the product card for order matching.
                    binding_ids=tuple(
                        value for value in (ozon_sku or product_id,) if value
                    ),
                    barcodes=tuple(
                        value
                        for value in (str(barcode).strip() for barcode in raw_barcodes)
                        if value
                    ),
                    sku_codes=tuple(value for value in (offer_id,) if value),
                    requirements={
                        "mandatory_mark": mandatory_mark,
                        "is_kiz": is_kiz,
                    },
                    raw_payload=product,
                )
            )
        delivery_method = raw.get("delivery_method")
        warehouse_id = (
            str(delivery_method.get("warehouse_id") or "").strip()
            if isinstance(delivery_method, dict)
            else ""
        )
        normalized.append(
            NormalizedMarketplaceOrder(
                external_order_id=posting_number,
                marketplace_status=str(
                    raw.get("status") or raw.get("status_alias") or ""
                ).strip().lower(),
                marketplace_substatus=str(
                    raw.get("substatus") or raw.get("status_transcription") or ""
                ).strip().lower(),
                ordered_at=str(raw.get("in_process_at") or raw.get("created_at") or "").strip(),
                cutoff_at=str(raw.get("cutoff") or raw.get("shipment_date") or "").strip(),
                warehouse_id=warehouse_id,
                items=tuple(items),
                raw_payload=raw,
            )
        )
    has_next = bool(container.get("has_next")) if isinstance(container, dict) else False
    cursor = str(container.get("cursor") or "").strip() if isinstance(container, dict) else ""
    return tuple(normalized), has_next, cursor


def _ozon_ship_product_id(item) -> int:
    raw_payload = item.raw_payload if isinstance(item.raw_payload, dict) else {}
    raw_product_id = raw_payload.get("product_id")
    identifier_name = "product_id"
    if raw_product_id in (None, ""):
        raw_product_id = raw_payload.get("sku")
        identifier_name = "sku"
    try:
        product_id = int(raw_product_id)
    except (TypeError, ValueError) as exc:
        raise FbsIntegrationError(
            f"Строка {item.external_line_id}: отсутствует числовой product_id или sku Ozon."
        ) from exc
    if product_id <= 0:
        raise FbsIntegrationError(
            f"{identifier_name} Ozon должен быть положительным числом."
        )
    return product_id


def build_ozon_ship_spec(order: FbsOrder) -> MarketplaceCommandSpec:
    if str(order.marketplace_status or "").strip().lower() != OZON_AWAITING_PACKAGING:
        raise FbsIntegrationError("Отправление Ozon не ожидает сборку.")
    products_by_id = {}
    for item in order.items.all():
        try:
            quantity = int(item.quantity)
        except (TypeError, ValueError) as exc:
            raise FbsIntegrationError(
                f"Строка {item.external_line_id}: неверное количество товара Ozon."
            ) from exc
        if quantity <= 0:
            raise FbsIntegrationError(
                f"Строка {item.external_line_id}: количество товара Ozon должно быть положительным."
            )
        product_id = _ozon_ship_product_id(item)
        products_by_id[product_id] = products_by_id.get(product_id, 0) + quantity
    if not products_by_id:
        raise FbsIntegrationError("В отправлении Ozon нет товаров для сборки.")
    products = [
        {"product_id": product_id, "quantity": products_by_id[product_id]}
        for product_id in sorted(products_by_id)
    ]
    return MarketplaceCommandSpec(
        command_type=OZON_SHIP_POSTING,
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/v4/posting/fbs/ship",
        endpoint_version="v4",
        query={},
        body={
            "posting_number": order.external_order_id,
            "packages": [{"products": products}],
            "with": {"additional_data": True},
        },
    )


def build_ozon_create_exemplars_spec(order: FbsOrder) -> MarketplaceCommandSpec:
    return MarketplaceCommandSpec(
        command_type=OZON_CREATE_OR_GET_EXEMPLARS,
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/v6/fbs/posting/product/exemplar/create-or-get",
        endpoint_version="v6",
        query={},
        body={"posting_number": order.external_order_id},
    )


def build_ozon_set_exemplars_spec(order: FbsOrder, body: dict) -> MarketplaceCommandSpec:
    if str(body.get("posting_number") or "") != str(order.external_order_id):
        raise FbsIntegrationError("Данные экземпляров относятся к другому отправлению Ozon.")
    if not isinstance(body.get("products"), list) or not body["products"]:
        raise FbsIntegrationError("Для Ozon не подготовлены экземпляры товаров.")
    return MarketplaceCommandSpec(
        command_type=OZON_SET_EXEMPLARS,
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/v6/fbs/posting/product/exemplar/set",
        endpoint_version="v6",
        query={},
        body=body,
    )


def build_ozon_exemplar_status_spec(order: FbsOrder) -> MarketplaceCommandSpec:
    return MarketplaceCommandSpec(
        command_type=OZON_READ_EXEMPLAR_STATUS,
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/v5/fbs/posting/product/exemplar/status",
        endpoint_version="v5",
        query={},
        body={"posting_number": order.external_order_id},
    )


def _ozon_exemplar_payload(order: FbsOrder, response: MarketplaceHttpResponse) -> dict:
    payload = response.json_payload
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if not isinstance(payload, dict):
        raise FbsIntegrationError("Ozon не вернул данные экземпляров.")
    posting_number = str(payload.get("posting_number") or "").strip()
    products = payload.get("products")
    if posting_number != order.external_order_id or not isinstance(products, list):
        raise FbsIntegrationError("Ответ Ozon по экземплярам относится к другому отправлению.")
    return payload


def parse_ozon_create_exemplars_response(
    order: FbsOrder,
    response: MarketplaceHttpResponse,
) -> dict:
    payload = _ozon_exemplar_payload(order, response)
    if not payload["products"]:
        raise FbsIntegrationError("Ozon не создал экземпляры товаров отправления.")
    return payload


def parse_ozon_set_exemplars_response(
    order: FbsOrder,
    response: MarketplaceHttpResponse,
) -> dict:
    return {"posting_number": order.external_order_id, "task_created": True}


def parse_ozon_exemplar_status_response(
    order: FbsOrder,
    response: MarketplaceHttpResponse,
) -> dict:
    payload = _ozon_exemplar_payload(order, response)
    payload["status"] = str(payload.get("status") or "").strip().lower()
    return payload


def build_ozon_readback_spec(order: FbsOrder) -> MarketplaceCommandSpec:
    return MarketplaceCommandSpec(
        command_type=OZON_READ_POSTING_AFTER_SHIP,
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/v3/posting/fbs/get",
        endpoint_version="v3",
        query={},
        body={"posting_number": order.external_order_id, "with": {"barcodes": True}},
    )


def build_ozon_label_spec(order: FbsOrder) -> MarketplaceCommandSpec:
    if str(order.marketplace_status or "").strip().lower() != OZON_AWAITING_DELIVER:
        raise FbsIntegrationError("Отправление Ozon еще не готово к получению этикетки.")
    return MarketplaceCommandSpec(
        command_type=OZON_FETCH_POSTING_LABEL,
        http_method=FbsMarketplaceCommand.METHOD_POST,
        endpoint="/v2/posting/fbs/package-label",
        endpoint_version="v2",
        query={},
        body={"posting_number": [order.external_order_id]},
    )


def parse_ozon_ship_response(order: FbsOrder, response: MarketplaceHttpResponse) -> dict:
    payload = response.json_payload
    result = payload.get("result") if isinstance(payload, dict) else None
    result_values = result if isinstance(result, list) else [result]
    if order.external_order_id not in {str(value) for value in result_values if value is not None}:
        raise FbsIntegrationError("Ozon не подтвердил сборку нужного отправления.")
    return {"posting_number": order.external_order_id}


def parse_ozon_readback_response(order: FbsOrder, response: MarketplaceHttpResponse) -> dict:
    payload = response.json_payload
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise FbsIntegrationError("Ozon не вернул данные отправления при сверке.")
    posting_number = str(result.get("posting_number") or "").strip()
    status = str(result.get("status") or "").strip()
    substatus = str(result.get("substatus") or "").strip()
    if posting_number != order.external_order_id or not status:
        raise FbsIntegrationError("Ответ сверки Ozon относится к другому отправлению.")
    barcodes = _ozon_barcodes(result)
    if status.lower() == OZON_AWAITING_DELIVER and not barcodes:
        raise FbsIntegrationError("Ozon не вернул штрихкод собранного отправления.")
    return {
        "posting_number": posting_number,
        "status": status,
        "substatus": substatus,
        "barcodes": barcodes,
    }


def _ozon_barcodes(payload: dict | None) -> dict[str, str]:
    raw_barcodes = payload.get("barcodes") if isinstance(payload, dict) else None
    if not isinstance(raw_barcodes, dict):
        return {}
    return {
        key: value
        for key in ("lower_barcode", "upper_barcode")
        if (value := str(raw_barcodes.get(key) or "").strip())
    }


def _ozon_order_label_barcodes(order: FbsOrder) -> tuple[str, ...]:
    raw_payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    latest_status = raw_payload.get("_latest_status")
    candidates = []
    for payload in (latest_status, raw_payload):
        barcodes = _ozon_barcodes(payload if isinstance(payload, dict) else None)
        candidates.extend(
            barcodes[key]
            for key in ("lower_barcode", "upper_barcode")
            if key in barcodes
        )
    return tuple(dict.fromkeys(candidates))


def parse_ozon_label_response(
    order: FbsOrder,
    response: MarketplaceHttpResponse,
) -> ParsedMarketplaceLabel:
    content = bytes(response.content or b"")
    if not content or len(content) > 20 * 1024 * 1024:
        raise FbsIntegrationError("Размер файла этикетки Ozon недопустим.")
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if "pdf" not in content_type and not content.startswith(b"%PDF"):
        raise FbsIntegrationError("Ozon вернул файл этикетки не в формате PDF.")
    barcodes = _ozon_order_label_barcodes(order)
    if not barcodes:
        raise FbsIntegrationError("Ozon не вернул штрихкод этикетки отправления.")
    return ParsedMarketplaceLabel(
        external_label_id=order.external_order_id,
        barcode=barcodes[0],
        label_format=FbsOrderLabel.FORMAT_PDF,
        file_name=f"ozon-{order.external_order_id}.pdf",
        content=content,
        metadata={
            "posting_number": order.external_order_id,
            "barcodes": list(barcodes),
        },
    )
