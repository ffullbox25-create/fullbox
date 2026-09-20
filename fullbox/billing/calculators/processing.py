from __future__ import annotations

from .base import ChargeCandidate
from .utils import at_least_one, first_number, list_sum, payload_of


def _service_code(data: dict, app_type: str) -> str:
    raw = " ".join(
        str(data.get(key) or "").lower()
        for key in ("service", "service_type", "operation", "operation_type", "title", "description")
    )
    if "перемарк" in raw or "remark" in raw:
        return "processing_marking_barcode_58x40"
    if "маркир" in raw or "mark" in raw or "чз" in raw:
        return "processing_marking_chz_58x40" if "чз" in raw else "processing_marking_barcode_75x120"
    if "стик" in raw or "stick" in raw:
        return "processing_marking_barcode_58x40"
    if "комплект" in raw or "kit" in raw:
        return "processing_kitting_upto_3"
    return "processing_packaging" if app_type == "packing" else "processing_packaging"


def calculate(application) -> list[ChargeCandidate]:
    data = payload_of(application)
    app_id = application.application_id
    qty = (
        list_sum(data, ("items", "rows", "products", "sku_rows"), ("actual_qty", "qty", "quantity", "count"))
        or first_number(data, ("actual_qty", "qty", "quantity", "items_count", "products_count"), default="0")
    )
    service_code = _service_code(data, application.application_type)
    return [
        ChargeCandidate(
            service_code=service_code,
            quantity=at_least_one(qty),
            operation_type=application.application_type,
            operation_id=app_id,
            source_key=f"{application.application_type}:{app_id}:{service_code}",
            comment=f"{application.get_application_type_display()} по заявке {app_id}",
        )
    ]
