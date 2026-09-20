def _parse_qty(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def extract_processing_items(payload: dict) -> list[dict]:
    payload = payload or {}
    items = {}

    stock_rows = payload.get("stock_rows") or []
    if not isinstance(stock_rows, list) or not stock_rows:
        cards = payload.get("cards") or []
        if isinstance(cards, list):
            for card in cards:
                if not isinstance(card, dict):
                    continue
                base_article = (card.get("article") or "").strip()
                for row in card.get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    stock_rows.append(
                        {
                            "article": row.get("article") or base_article,
                            "size": row.get("size") or "",
                            "barcode": row.get("barcode") or "",
                            "qty": row.get("qty"),
                        }
                    )
    if not isinstance(stock_rows, list) or not stock_rows:
        stock_rows = payload.get("size_rows") or []

    for row in stock_rows:
        if not isinstance(row, dict):
            continue
        sku_code = str(row.get("article") or row.get("sku") or payload.get("article") or "").strip()
        if not sku_code:
            continue
        size = str(row.get("size") or row.get("size_value") or "").strip()
        barcode = str(row.get("barcode") or "").strip()
        qty = _parse_qty(row.get("qty") or row.get("recount_qty"))
        key = (sku_code, size)
        if key not in items:
            items[key] = {
                "sku_code": sku_code,
                "size": size,
                "barcode": barcode,
                "qty": qty or 0,
            }
        else:
            items[key]["qty"] += qty or 0
            if not items[key]["barcode"] and barcode:
                items[key]["barcode"] = barcode

    return list(items.values())
