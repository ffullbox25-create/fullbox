from __future__ import annotations

from dataclasses import dataclass

from fbs.exceptions import FbsIntegrationError
from fbs.integrations.http import RequestsMarketplaceReadTransport
from fbs.integrations.ozon import (
    OZON_HANDOVER_DOCUMENT_BARCODE,
    OZON_HANDOVER_DOCUMENT_PDF,
    build_ozon_carriage_list_spec,
    build_ozon_carriage_postings_spec,
    build_ozon_handover_document_spec,
    parse_ozon_carriage_list,
    parse_ozon_carriage_postings,
    parse_ozon_handover_document_response,
)
from fbs.models import (
    FbsHandoverBatch,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
)


@dataclass(frozen=True)
class OzonHandoverDocument:
    content: bytes
    content_type: str
    filename: str
    carriage_id: int


def _checked_response(response, *, operation: str):
    if not 200 <= int(response.status_code or 0) < 300:
        raise FbsIntegrationError(
            f"Ozon не выполнил операцию «{operation}» (HTTP {response.status_code})."
        )
    return response


def _exact_batch_orders(batch: FbsHandoverBatch):
    assignment_order_ids = set(
        batch.order_assignments.exclude(
            status=FbsHandoverOrderAssignment.STATUS_CANCELED
        ).values_list("order_id", flat=True)
    )
    active_order_ids = set(
        FbsHandoverOrder.objects.filter(
            box__batch=batch,
            status=FbsHandoverOrder.STATUS_ACTIVE,
        ).values_list("order_id", flat=True)
    )
    if not assignment_order_ids or assignment_order_ids != active_order_ids:
        raise FbsIntegrationError(
            "Нельзя сопоставить документы Ozon: подтверждённый состав отгрузки "
            "не совпадает с составом физических коробов."
        )
    return list(
        batch.order_assignments.exclude(
            status=FbsHandoverOrderAssignment.STATUS_CANCELED
        )
        .select_related("order")
        .filter(order_id__in=active_order_ids)
        .order_by("order_id")
    )


def _batch_ozon_identity(batch: FbsHandoverBatch) -> tuple[set[str], str, str]:
    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_OZON:
        raise FbsIntegrationError("Документы Ozon доступны только для отгрузки Ozon.")
    assignments = _exact_batch_orders(batch)
    posting_numbers = {
        str(assignment.order.external_order_id or "").strip()
        for assignment in assignments
    }
    if "" in posting_numbers:
        raise FbsIntegrationError("В составе отгрузки есть заказ Ozon без номера.")
    delivery_method_ids = set()
    departure_dates = set()
    for assignment in assignments:
        payload = (
            assignment.order.raw_payload
            if isinstance(assignment.order.raw_payload, dict)
            else {}
        )
        delivery_method = payload.get("delivery_method")
        delivery_method = delivery_method if isinstance(delivery_method, dict) else {}
        delivery_method_id = str(
            delivery_method.get("id")
            or delivery_method.get("warehouse_id")
            or ""
        ).strip()
        shipment_date = str(payload.get("shipment_date") or "").strip()[:10]
        if delivery_method_id:
            delivery_method_ids.add(delivery_method_id)
        if shipment_date:
            departure_dates.add(shipment_date)
    if len(delivery_method_ids) != 1 or not next(iter(delivery_method_ids)).isdigit():
        raise FbsIntegrationError(
            "Нельзя определить один метод доставки Ozon для всей отгрузки."
        )
    if len(departure_dates) != 1:
        raise FbsIntegrationError(
            "Нельзя определить одну дату передачи Ozon для всей отгрузки."
        )
    return posting_numbers, next(iter(delivery_method_ids)), next(iter(departure_dates))


def find_exact_ozon_carriage(
    batch: FbsHandoverBatch,
    *,
    transport=None,
) -> int:
    """Find one existing Ozon carriage whose postings exactly equal the batch."""
    posting_numbers, delivery_method_id, departure_date = _batch_ozon_identity(batch)
    own_transport = transport is None
    transport = transport or RequestsMarketplaceReadTransport(reuse_connections=True)
    exact_matches = []
    try:
        response = _checked_response(
            transport.send(
                batch.profile,
                build_ozon_carriage_list_spec(departure_date=departure_date),
            ),
            operation="список перевозок",
        )
        methods = parse_ozon_carriage_list(response.json_payload)
        candidate_ids = []
        for method in methods:
            if method["delivery_method_id"] != delivery_method_id:
                continue
            for carriage in method["carriages"]:
                try:
                    carriage_id = int(carriage.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if carriage_id > 0 and carriage_id not in candidate_ids:
                    candidate_ids.append(carriage_id)
        for carriage_id in candidate_ids:
            response = _checked_response(
                transport.send(
                    batch.profile,
                    build_ozon_carriage_postings_spec(carriage_id),
                ),
                operation=f"состав перевозки {carriage_id}",
            )
            carriage_postings = parse_ozon_carriage_postings(response.json_payload)
            if len(carriage_postings) == len(set(carriage_postings)) and set(
                carriage_postings
            ) == posting_numbers:
                exact_matches.append(carriage_id)
    finally:
        if own_transport:
            transport.close()
    if not exact_matches:
        raise FbsIntegrationError(
            "Ozon не нашёл перевозку с точным составом этой отгрузки. "
            "Внутреннюю этикетку Fullbox можно печатать; официальные документы "
            "появятся после формирования перевозки в Ozon."
        )
    if len(exact_matches) != 1:
        raise FbsIntegrationError(
            "Ozon вернул несколько перевозок с одинаковым составом. "
            "Автоматическая загрузка остановлена для безопасной проверки."
        )
    return exact_matches[0]


def download_exact_ozon_handover_document(
    *, batch_id: int, document_kind: str, transport=None
) -> OzonHandoverDocument:
    if document_kind not in {
        OZON_HANDOVER_DOCUMENT_BARCODE,
        OZON_HANDOVER_DOCUMENT_PDF,
    }:
        raise FbsIntegrationError("Неизвестный тип документа Ozon.")
    batch = (
        FbsHandoverBatch.objects.select_related("profile")
        .filter(pk=batch_id)
        .first()
    )
    if batch is None:
        raise FbsIntegrationError("Отгрузка FBS не найдена.")
    own_transport = transport is None
    transport = transport or RequestsMarketplaceReadTransport(reuse_connections=True)
    try:
        carriage_id = find_exact_ozon_carriage(batch, transport=transport)
        response = transport.send(
            batch.profile,
            build_ozon_handover_document_spec(
                carriage_id=carriage_id,
                document_kind=document_kind,
            ),
        )
        content, content_type = parse_ozon_handover_document_response(
            response,
            document_kind=document_kind,
        )
    finally:
        if own_transport:
            transport.close()
    extension = "png" if document_kind == OZON_HANDOVER_DOCUMENT_BARCODE else "pdf"
    return OzonHandoverDocument(
        content=content,
        content_type=content_type,
        filename=f"ozon-fbs-{batch.id}-{carriage_id}.{extension}",
        carriage_id=carriage_id,
    )
