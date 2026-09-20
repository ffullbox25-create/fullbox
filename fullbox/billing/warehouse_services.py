"""Факты услуг кладовщика (услуга + qty без цены) → биллинг менеджера."""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
import re
from typing import Any
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_date, parse_datetime
from django.utils import timezone

from sku.models import Agency

from .calculators.base import ChargeCandidate
from .models import BillingApplication, BillingService, ClientTariffItem, WarehouseServiceFact
from .standard_price_catalog import SECTION_TO_CATEGORY
from .tariff_services import active_tariff_version

# Разделы стандартного прайса, доступные кладовщику по типу операции.
PROCESS_SECTIONS: dict[str, set[str]] = {
    WarehouseServiceFact.ORDER_RECEIVING: {"01", "02"},
    WarehouseServiceFact.ORDER_PROCESSING: {"03", "04", "05", "06", "07", "09"},
    WarehouseServiceFact.ORDER_PACKING: {"03", "04", "05", "06", "07", "09"},
    WarehouseServiceFact.ORDER_SHIPPING: {"08", "09", "10"},
    BillingApplication.TYPE_LOGISTICS: {"11"},
    BillingApplication.TYPE_OTHER: {"01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "12"},
    WarehouseServiceFact.ORDER_FBS: set(),
}

PROCESS_CATEGORY_CODES: dict[str, set[str]] = {
    WarehouseServiceFact.ORDER_RECEIVING: {"receiving"},
    WarehouseServiceFact.ORDER_PROCESSING: {"marking", "packing", "processing"},
    WarehouseServiceFact.ORDER_PACKING: {"marking", "packing", "processing"},
    WarehouseServiceFact.ORDER_SHIPPING: {"shipping", "marking"},
    BillingApplication.TYPE_LOGISTICS: {"logistics"},
    BillingApplication.TYPE_OTHER: {"receiving", "marking", "packing", "processing", "shipping", "extra", "other"},
    WarehouseServiceFact.ORDER_FBS: {
        "fbs_processing",
        "fbs_receiving",
        "fbs_picking",
        "fbs_shipping",
        "fbs_delivery",
        "fbs_storage",
    },
}

WAREHOUSE_ROLES = {
    "storekeeper",
    "processing",
    "processing_head",
    "shipping",
    "manager",
    "head_manager",
    "admin",
    "director",
    "developer",
    "driver",
    "logistician",
}

COMPLETION_FACT_STATUSES = {
    WarehouseServiceFact.STATUS_SENT_TO_BILLING,
    WarehouseServiceFact.STATUS_WAITING_MANAGER_REVIEW,
    WarehouseServiceFact.STATUS_APPROVED,
    WarehouseServiceFact.STATUS_CHARGED,
}

SHIPPING_PICK_AUTO_SOURCE = "shipping_auto_pick"
SHIPPING_PICK_MANUAL_CONFIRMATION_SOURCE = "shipping_manual_confirmation"
SHIPPING_PICK_MANUAL_INPUT_SOURCES = {
    "shipping_loading_manual",
    SHIPPING_PICK_MANUAL_CONFIRMATION_SOURCE,
}
SHIPPING_PICK_PIECE_SERVICE_CODE = "shipping_pick_storage_item"
SHIPPING_PICK_BOX_BANDS = (
    ("shipping_pick_box_1_15kg", 15_000, "1-15 кг"),
    ("shipping_pick_box_15_30kg", 30_000, "15-30 кг"),
    ("shipping_pick_box_30_50kg", 50_000, "30-50 кг"),
)
SHIPPING_PICK_AUTO_SERVICE_CODES = {
    SHIPPING_PICK_PIECE_SERVICE_CODE,
    *(code for code, _max_weight, _label in SHIPPING_PICK_BOX_BANDS),
}

UNLISTED_SERVICE_SOURCE = "warehouse_unlisted"
UNLISTED_SERVICE_METADATA_KEY = "unlisted_service"


def normalize_order_type(order_type: str) -> str:
    raw = str(order_type or "").strip().lower()
    if raw in {"packing", "pack"}:
        return WarehouseServiceFact.ORDER_PACKING
    if raw in {"processing", "process"}:
        return WarehouseServiceFact.ORDER_PROCESSING
    if raw in {"receiving", "receipt"}:
        return WarehouseServiceFact.ORDER_RECEIVING
    if raw in {"shipping", "ship", "otg"}:
        return WarehouseServiceFact.ORDER_SHIPPING
    if raw in {"logistics", "trip", "route"}:
        return BillingApplication.TYPE_LOGISTICS
    if raw in {"other", "oth", "extra"}:
        return BillingApplication.TYPE_OTHER
    if raw in {"fbs", "fbs_movement", "fbs_order", "fbs_storage"}:
        return WarehouseServiceFact.ORDER_FBS
    raise ValidationError("Неизвестный тип заявки.")


def compatible_warehouse_order_types(order_type: str) -> set[str]:
    """Допустимые эквиваленты типа без перехода между складскими процессами."""
    try:
        normalized = normalize_order_type(order_type)
    except ValidationError:
        # Биллинг вызывает selectors и для прочих типов заявок. Для них
        # складских фактов нет, поэтому безопасный результат — пустая группа.
        return set()
    if normalized in {
        WarehouseServiceFact.ORDER_PROCESSING,
        WarehouseServiceFact.ORDER_PACKING,
    }:
        return {
            WarehouseServiceFact.ORDER_PROCESSING,
            WarehouseServiceFact.ORDER_PACKING,
        }
    return {normalized}


def resolve_canonical_warehouse_order(
    *,
    client: Agency,
    order_type: str,
    order_id: str,
) -> tuple[str, str]:
    """Подтвердить, что заявка существует именно у указанного клиента."""
    normalized = normalize_order_type(order_type)
    normalized_order_id = str(order_id or "").strip()
    if not client or not getattr(client, "pk", None) or not normalized_order_id:
        raise ValidationError("Укажите клиента и номер заявки.")

    if normalized == WarehouseServiceFact.ORDER_FBS:
        exists = BillingApplication.objects.filter(
            application_type=BillingApplication.TYPE_FBS,
            application_id=normalized_order_id,
            client=client,
        ).exists()
    elif normalized == WarehouseServiceFact.ORDER_SHIPPING:
        from shipping.models import ShippingOrder

        exists = ShippingOrder.objects.filter(
            number=normalized_order_id,
            agency_id=client.pk,
        ).exists()
    elif normalized == BillingApplication.TYPE_LOGISTICS:
        from logistics.models import LogisticsTrip

        trip = (
            LogisticsTrip.objects.select_related("external_details__client")
            .filter(number=normalized_order_id)
            .first()
        )
        if trip is None:
            exists = False
        elif trip.trip_kind == LogisticsTrip.KIND_EXTERNAL:
            details = getattr(trip, "external_details", None)
            exists = bool(details and details.client_id == client.pk)
        else:
            exists = trip.orders.filter(shipping_order__agency_id=client.pk).exists()
    elif normalized == BillingApplication.TYPE_OTHER:
        from client_cabinet.models import OtherRequest

        exists = OtherRequest.objects.filter(
            public_number=normalized_order_id,
            agency_id=client.pk,
        ).exists()
    else:
        from audit.models import OrderAuditEntry

        owner_ids = list(
            OrderAuditEntry.objects.filter(
                order_id=normalized_order_id,
                order_type__in=compatible_warehouse_order_types(normalized),
            )
            .exclude(agency_id__isnull=True)
            .order_by()
            .values_list("agency_id", flat=True)
            .distinct()[:2]
        )
        # Если история когда-либо указывает двух клиентов, автоматически
        # выбирать одного нельзя: процесс должен быть сначала сверен вручную.
        exists = len(owner_ids) == 1 and owner_ids[0] == client.pk
    if not exists:
        # Одинаковый ответ для отсутствующей и чужой заявки не раскрывает
        # сотруднику существование номера в портфеле другого клиента.
        raise ValidationError("Заявка клиента не найдена.")
    return normalized, normalized_order_id


def _compatible_billing_application(
    *,
    client: Agency,
    order_type: str,
    order_id: str,
) -> BillingApplication | None:
    """Найти только заявку того же процесса (processing/packing — одна группа)."""
    compatible_types = compatible_warehouse_order_types(order_type)
    preferred_types = [order_type, *(value for value in sorted(compatible_types) if value != order_type)]
    for application_type in preferred_types:
        application = BillingApplication.objects.filter(
            client=client,
            application_type=application_type,
            application_id=order_id,
        ).first()
        if application:
            return application
    return None


def _application_matches_warehouse_order(
    application: BillingApplication,
    *,
    client: Agency,
    order_type: str,
    order_id: str,
) -> bool:
    return bool(
        application.client_id == client.pk
        and str(application.application_id or "").strip() == order_id
        and application.application_type in compatible_warehouse_order_types(order_type)
    )


def _unit_label(item: ClientTariffItem) -> str:
    unit = getattr(item, "unit", None)
    if unit is None:
        return str(getattr(item.service, "unit", "") or "шт")
    return str(getattr(unit, "short_name", None) or getattr(unit, "name", None) or item.service.unit or "шт")


def list_agreed_services_for_storekeeper(
    client: Agency,
    *,
    process_type: str,
    on_date=None,
) -> list[dict[str, Any]]:
    """Согласованные услуги клиента для склада: код/название/ед., без цен."""
    order_type = normalize_order_type(process_type)
    version = active_tariff_version(client, on_date=on_date or timezone.localdate())
    if not version:
        return []

    allowed_categories = PROCESS_CATEGORY_CODES.get(order_type) or set()
    allowed_sections = PROCESS_SECTIONS.get(order_type) or set()
    items = (
        ClientTariffItem.objects.filter(tariff_version=version, is_active=True)
        .select_related("service", "unit", "category", "service__standard_price")
        .order_by("category__sort_order", "sort_order", "service_name", "id")
    )
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in items:
        service = item.service
        if not service or getattr(service, "is_active", True) is False or service.id in seen:
            continue
        cat_code = str(getattr(item.category, "code", "") or "")
        section = str(getattr(getattr(service, "standard_price", None), "section_code", "") or "")
        code = str(service.code or "")
        by_category = bool(cat_code and cat_code in allowed_categories)
        by_section = bool(section and section in allowed_sections)
        by_prefix = False
        if order_type == WarehouseServiceFact.ORDER_RECEIVING:
            by_prefix = code.startswith("receiving") or code.startswith("vehicle_entry")
        elif order_type in {WarehouseServiceFact.ORDER_PROCESSING, WarehouseServiceFact.ORDER_PACKING}:
            by_prefix = code.startswith("processing") or code.startswith("print_")
        elif order_type == WarehouseServiceFact.ORDER_SHIPPING:
            by_prefix = code.startswith("shipping") or code.startswith("print_")
        elif order_type == BillingApplication.TYPE_LOGISTICS:
            by_prefix = code.startswith("logistics")
        elif order_type == BillingApplication.TYPE_OTHER:
            by_prefix = code.startswith(("extra_", "movement_", "returns_"))
        elif order_type == WarehouseServiceFact.ORDER_FBS:
            by_prefix = code.startswith("fbs_")
        if not (by_category or by_section or by_prefix):
            continue
        seen.add(service.id)
        rows.append(
            {
                "service_id": service.id,
                "code": service.code,
                "name": item.service_name or service.name,
                "unit": _unit_label(item),
                "category": getattr(item.category, "name", "") or "",
                "category_code": cat_code,
            }
        )
    return rows


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _normalized_box_code(value: Any) -> str:
    return str(value or "").strip().upper()


def _payload_box_codes(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {code for item in value if (code := _normalized_box_code(item))}
    code = _normalized_box_code(value)
    return {code} if code else set()


def shipping_pick_auto_quantities(*, client: Agency, order_id: str) -> dict[str, Any]:
    """Посчитать фактический подбор отгрузки по неизменяемым складским событиям."""
    _order_type, normalized_order_id = resolve_canonical_warehouse_order(
        client=client,
        order_type=WarehouseServiceFact.ORDER_SHIPPING,
        order_id=order_id,
    )

    from sklad.models import WarehouseEvent

    events = list(
        WarehouseEvent.objects.filter(
            agency=client,
            event_type="otg_arrived",
            stock_context_type="shipping",
            stock_context_id=normalized_order_id,
        )
        .select_related("container")
        .order_by("occurred_at", "id")
    )
    has_pick_markers = any(
        (
            event.payload.get("partial_shipping_pick") is True
            or event.payload.get("whole_box_shipping_pick") is True
        )
        for event in events
        if isinstance(event.payload, dict)
    )
    opened_box_codes: set[str] = set()
    opened_container_ids: set[int] = set()
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("partial_shipping_pick") is not True:
            continue
        opened_box_codes.update(_payload_box_codes(payload.get("source_box_code")))
        opened_box_codes.update(_payload_box_codes(payload.get("source_box_codes")))
        opened_box_codes.update(_payload_box_codes(payload.get("box_code")))
        container_code = _normalized_box_code(getattr(event.container, "container_code", ""))
        if container_code:
            opened_box_codes.add(container_code)
        if event.container_id:
            opened_container_ids.add(int(event.container_id))

    quantities = {code: 0 for code, _max_weight_g, _label in SHIPPING_PICK_BOX_BANDS}
    quantities[SHIPPING_PICK_PIECE_SERVICE_CODE] = 0
    performed_at_by_code = {code: None for code in quantities}
    boxes: dict[str, dict[str, Any]] = {}
    excluded_opened_boxes: dict[str, str] = {}
    piece_quantity = 0
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("partial_shipping_pick") is True:
            piece_quantity += int(event.qty or 0)
            current = performed_at_by_code[SHIPPING_PICK_PIECE_SERVICE_CODE]
            if current is None or event.occurred_at > current:
                performed_at_by_code[SHIPPING_PICK_PIECE_SERVICE_CODE] = event.occurred_at
        is_whole_box_pick = payload.get("whole_box_shipping_pick") is True
        if not is_whole_box_pick and not has_pick_markers and event.container_id:
            is_whole_box_pick = True
        if not is_whole_box_pick:
            continue
        box_code = str(
            payload.get("box_code")
            or getattr(event.container, "container_code", "")
            or ""
        ).strip()
        normalized_box_code = _normalized_box_code(box_code)
        if (normalized_box_code and normalized_box_code in opened_box_codes) or (
            event.container_id and int(event.container_id) in opened_container_ids
        ):
            if normalized_box_code:
                excluded_key = normalized_box_code
            elif event.container_id:
                excluded_key = f"container:{event.container_id}"
            else:
                excluded_key = f"event:{event.pk}"
            excluded_opened_boxes[excluded_key] = box_code or f"event-{event.pk}"
            piece_quantity += int(event.qty or 0)
            current = performed_at_by_code[SHIPPING_PICK_PIECE_SERVICE_CODE]
            if current is None or event.occurred_at > current:
                performed_at_by_code[SHIPPING_PICK_PIECE_SERVICE_CODE] = event.occurred_at
            continue
        if event.container_id:
            box_key = f"container:{event.container_id}"
        elif box_code:
            box_key = f"code:{normalized_box_code}"
        else:
            box_key = f"event:{event.pk}"
        if box_key in boxes:
            continue
        weight_g = _positive_int(getattr(event.container, "gross_weight_g", None))
        if weight_g is None:
            weight_g = _positive_int(payload.get("gross_weight_g") or payload.get("box_gross_weight_g"))
        boxes[box_key] = {
            "box_code": box_code or f"event-{event.pk}",
            "weight_g": weight_g,
            "occurred_at": event.occurred_at,
        }

    unclassified_boxes = []
    for box in boxes.values():
        weight_g = box["weight_g"]
        service_code = None
        if weight_g is not None:
            for code, max_weight_g, _label in SHIPPING_PICK_BOX_BANDS:
                if weight_g <= max_weight_g:
                    service_code = code
                    break
        if service_code:
            quantities[service_code] += 1
            current = performed_at_by_code[service_code]
            if current is None or box["occurred_at"] > current:
                performed_at_by_code[service_code] = box["occurred_at"]
        else:
            unclassified_boxes.append(box)
    quantities[SHIPPING_PICK_PIECE_SERVICE_CODE] = piece_quantity

    warnings = []
    if excluded_opened_boxes:
        preview = ", ".join(str(code) for code in list(excluded_opened_boxes.values())[:5])
        suffix = "" if len(excluded_opened_boxes) <= 5 else f" и еще {len(excluded_opened_boxes) - 5}"
        warnings.append(
            "Не отнесены к коробному подбору короба, из которых был частичный отбор: "
            f"{preview}{suffix}. Они учитываются как поштучный подбор."
        )
    if unclassified_boxes:
        preview = ", ".join(str(box["box_code"]) for box in unclassified_boxes[:5])
        suffix = "" if len(unclassified_boxes) <= 5 else f" и еще {len(unclassified_boxes) - 5}"
        warnings.append(
            "Не рассчитан подбор по коробам без корректного веса или тяжелее 50 кг: "
            f"{preview}{suffix}. Укажите вес короба и повторите расчет."
        )
    return {
        "order_id": normalized_order_id,
        "quantities": quantities,
        "total_boxes": len(boxes),
        "classified_boxes": len(boxes) - len(unclassified_boxes),
        "unclassified_boxes": len(unclassified_boxes),
        "unclassified_box_codes": [box["box_code"] for box in unclassified_boxes],
        "excluded_opened_boxes": len(excluded_opened_boxes),
        "excluded_opened_box_codes": list(excluded_opened_boxes.values()),
        "piece_quantity": piece_quantity,
        "performed_at_by_code": performed_at_by_code,
        "warnings": warnings,
    }


def shipping_pick_auto_service_suggestions(
    *,
    client: Agency,
    order_id: str,
    agreed_services: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Сопоставить складской факт подбора с услугами активного тарифа клиента."""
    result = shipping_pick_auto_quantities(client=client, order_id=order_id)
    agreed_services = agreed_services if agreed_services is not None else list_agreed_services_for_storekeeper(
        client,
        process_type=WarehouseServiceFact.ORDER_SHIPPING,
    )
    agreed_by_code = {
        str(row.get("code") or ""): row
        for row in agreed_services
        if str(row.get("code") or "") in SHIPPING_PICK_AUTO_SERVICE_CODES
    }
    automatic_facts = []
    warnings = list(result["warnings"])
    for code, quantity in result["quantities"].items():
        if quantity <= 0:
            continue
        service = agreed_by_code.get(code)
        if not service:
            warnings.append(
                f"Автоматически найден объем услуги {code}: {quantity}, но услуга отсутствует в активном тарифе клиента."
            )
            continue
        automatic_facts.append(
            {
                "service_id": int(service["service_id"]),
                "code": code,
                "name": service.get("name") or code,
                "planned_quantity": str(quantity),
                "quantity": str(quantity),
                "auto_quantity": str(quantity),
                "unit": service.get("unit") or "шт",
                "comment": "Рассчитано по фактическим складским событиям подбора",
                "discrepancy_reason": "",
                "source": SHIPPING_PICK_AUTO_SOURCE,
                "is_automatic": True,
                "performed_at": result["performed_at_by_code"][code].isoformat()
                if result["performed_at_by_code"][code]
                else "",
                "performed_at_iso": timezone.localtime(result["performed_at_by_code"][code]).strftime(
                    "%Y-%m-%dT%H:%M"
                )
                if result["performed_at_by_code"][code]
                else "",
                "metadata": {
                    "auto_quantity": str(quantity),
                    "calculation": "warehouse_shipping_pick_events",
                },
            }
        )

    band_rows = []
    for code, _max_weight_g, label in SHIPPING_PICK_BOX_BANDS:
        band_rows.append({"code": code, "label": label, "quantity": result["quantities"][code]})
    return {
        "automatic_facts": automatic_facts,
        "automatic_service_ids": [int(row["service_id"]) for row in agreed_by_code.values()],
        "automatic_summary": {
            "total_boxes": result["total_boxes"],
            "classified_boxes": result["classified_boxes"],
            "unclassified_boxes": result["unclassified_boxes"],
            "excluded_opened_boxes": result["excluded_opened_boxes"],
            "piece_quantity": result["piece_quantity"],
            "box_bands": band_rows,
        },
        "automatic_warnings": warnings,
    }


def list_facts(*, client: Agency, order_type: str, order_id: str) -> list[WarehouseServiceFact]:
    order_type, order_id = resolve_canonical_warehouse_order(
        client=client,
        order_type=order_type,
        order_id=order_id,
    )
    return list(
        WarehouseServiceFact.objects.filter(
            client=client,
            order_id=order_id,
            order_type__in=compatible_warehouse_order_types(order_type),
        )
        .select_related("service", "reported_by")
        .order_by("id")
    )


def completion_facts(
    *,
    client: Agency,
    order_type: str,
    order_id: str,
) -> list[WarehouseServiceFact]:
    """Вернуть сохраненные факты, которые разрешают завершить операцию."""
    normalized_type = normalize_order_type(order_type)
    return list(
        WarehouseServiceFact.objects.filter(
            client=client,
            order_type__in=compatible_warehouse_order_types(normalized_type),
            order_id=str(order_id or "").strip(),
            quantity__gt=0,
            status__in=COMPLETION_FACT_STATUSES,
        )
        .select_related("service", "reported_by")
        .order_by("id")
    )


def require_completion_facts(
    *,
    client: Agency,
    order_type: str,
    order_id: str,
) -> list[WarehouseServiceFact]:
    facts = completion_facts(client=client, order_type=order_type, order_id=order_id)
    if not facts:
        raise ValidationError(
            "Перед завершением укажите хотя бы одну фактически оказанную услугу с количеством."
        )
    return facts


def facts_payload(facts: list[WarehouseServiceFact]) -> list[dict[str, Any]]:
    rows = []
    for fact in facts:
        metadata = fact.metadata if isinstance(fact.metadata, dict) else {}
        manager_review = metadata.get("manager_review") if isinstance(metadata.get("manager_review"), dict) else {}
        reported_by = ""
        if fact.reported_by:
            reported_by = (
                fact.reported_by.get_full_name().strip()
                or fact.reported_by.get_username()
                or str(fact.reported_by)
            )
        rows.append(
            {
                "id": fact.id,
                "service_id": fact.service_id,
                "code": getattr(fact.service, "code", "") or "",
                "name": fact.service_name_snapshot or getattr(fact.service, "name", "") or "",
                "is_unlisted": bool(metadata.get(UNLISTED_SERVICE_METADATA_KEY)),
                "custom_key": str(metadata.get("custom_key") or ""),
                "custom_name": str(
                    metadata.get("original_custom_name")
                    or fact.service_name_snapshot
                    or getattr(fact.service, "name", "")
                    or ""
                ),
                "manager_comment": str(manager_review.get("comment") or ""),
                "quantity": str(fact.quantity),
                "planned_quantity": str(fact.planned_quantity) if fact.planned_quantity is not None else "",
                "unit": fact.unit or getattr(fact.service, "unit", "") or "шт",
                "comment": fact.comment or "",
                "discrepancy_reason": fact.discrepancy_reason or "",
                "status": fact.status,
                "status_label": fact.get_status_display(),
                "source": fact.source or "",
                "auto_quantity": str(metadata.get("auto_quantity") or ""),
                "warehouse_label": fact.warehouse_label or "",
                "performed_at": timezone.localtime(fact.performed_at).strftime("%d.%m.%Y %H:%M")
                if fact.performed_at
                else "",
                "performed_at_iso": timezone.localtime(fact.performed_at).strftime("%Y-%m-%dT%H:%M")
                if fact.performed_at
                else "",
                "reported_by": reported_by,
                "reported_at": timezone.localtime(fact.reported_at).strftime("%d.%m.%Y %H:%M")
                if fact.reported_at
                else "",
            }
        )
    return rows


def _optional_decimal(raw_value: Any) -> Decimal | None:
    if raw_value is None or raw_value == "":
        return None
    try:
        return Decimal(str(raw_value))
    except Exception as exc:
        raise ValidationError("Некорректное плановое количество.") from exc


def _line_service_id(raw: dict[str, Any]) -> int:
    try:
        return int(raw.get("service_id") or raw.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _normalized_custom_key(raw_value: Any) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]", "", str(raw_value or ""))[:64]
    return value or uuid4().hex


def _unlisted_source_key(*, client_id: int, order_type: str, order_id: str, custom_key: str) -> str:
    return (
        f"warehouse_unlisted:{order_type}:{client_id}:"
        f"{str(order_id)[:48]}:{str(custom_key)[:48]}"
    )[:160]


def _fact_identity(fact: WarehouseServiceFact) -> tuple[str, Any]:
    metadata = fact.metadata if isinstance(fact.metadata, dict) else {}
    custom_key = str(metadata.get("custom_key") or "").strip()
    if metadata.get(UNLISTED_SERVICE_METADATA_KEY) and custom_key:
        return ("custom", custom_key)
    if fact.service_id:
        return ("service", fact.service_id)
    return ("fact", fact.pk)


def _optional_datetime(raw_value: Any):
    if not raw_value:
        return None
    if hasattr(raw_value, "date"):
        value = raw_value
    else:
        text = str(raw_value).strip()
        value = parse_datetime(text)
        if value is None:
            parsed_date = parse_date(text)
            if parsed_date:
                value = datetime.combine(parsed_date, time.min)
    if value is None:
        raise ValidationError("Некорректная дата выполнения услуги.")
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


@transaction.atomic
def replace_warehouse_facts(
    *,
    client: Agency,
    order_type: str,
    order_id: str,
    lines: list[dict[str, Any]],
    user=None,
) -> list[WarehouseServiceFact]:
    """Синхронизировать факты по заявке без потери истории и вложений.

    Отсутствующая в новой передаче строка не удаляется: она помечается отменённой.
    Связанный с ней черновой расчёт исключается, а финансово зафиксированный факт
    изменить нельзя. Цены из склада по-прежнему не принимаются и не хранятся.
    """
    order_type, order_id = resolve_canonical_warehouse_order(
        client=client,
        order_type=order_type,
        order_id=order_id,
    )

    allowed = {
        int(row["service_id"]): row
        for row in list_agreed_services_for_storekeeper(client, process_type=order_type)
    }
    if order_type == WarehouseServiceFact.ORDER_SHIPPING:
        automatic = shipping_pick_auto_service_suggestions(
            client=client,
            order_id=order_id,
            agreed_services=list(allowed.values()),
        )
        automatic_service_ids = set(automatic["automatic_service_ids"])
        submitted_automatic: dict[int, dict[str, Any]] = {}
        for row in lines or []:
            service_id = _line_service_id(row)
            if service_id not in automatic_service_ids:
                continue
            source = str(row.get("source") or "").strip()
            if source not in SHIPPING_PICK_MANUAL_INPUT_SOURCES:
                continue
            if service_id in submitted_automatic:
                raise ValidationError("Одна и та же услуга не может быть указана дважды.")
            submitted_automatic[service_id] = row
        lines = [
            row
            for row in (lines or [])
            if _line_service_id(row) not in automatic_service_ids
        ]
        for automatic_fact in automatic["automatic_facts"]:
            service_id = _line_service_id(automatic_fact)
            submitted = submitted_automatic.get(service_id)
            if not submitted:
                lines.append(automatic_fact)
                continue
            try:
                submitted_quantity = Decimal(str(submitted.get("quantity") or "0"))
            except Exception as exc:
                raise ValidationError("Некорректное фактическое количество услуги.") from exc
            if submitted_quantity <= 0:
                raise ValidationError("Укажите фактическое количество услуги больше нуля.")
            confirmed_fact = dict(automatic_fact)
            confirmed_fact.update(
                {
                    "quantity": str(submitted_quantity),
                    "planned_quantity": automatic_fact.get("planned_quantity"),
                    "auto_quantity": automatic_fact.get("auto_quantity"),
                    "discrepancy_reason": submitted.get("discrepancy_reason") or "",
                    "comment": submitted.get("comment") or automatic_fact.get("comment") or "",
                    "performed_at": submitted.get("performed_at") or automatic_fact.get("performed_at"),
                    "source": SHIPPING_PICK_MANUAL_CONFIRMATION_SOURCE,
                }
            )
            lines.append(confirmed_fact)
    cleaned: list[dict[str, Any]] = []
    seen_identities: set[tuple[str, Any]] = set()
    for raw in lines or []:
        service_id = _line_service_id(raw)
        custom_name = str(raw.get("custom_name") or "").strip()[:255]
        is_unlisted = not service_id and bool(custom_name)
        if is_unlisted and order_type not in {
            WarehouseServiceFact.ORDER_RECEIVING,
            WarehouseServiceFact.ORDER_PROCESSING,
        }:
            raise ValidationError("Услугу вне списка можно указать только на приемке и обработке.")
        if not is_unlisted and service_id not in allowed:
            raise ValidationError("Услуга не входит в согласованный тариф клиента для этого раздела.")
        try:
            qty = Decimal(str(raw.get("quantity") or "0"))
        except Exception as exc:
            raise ValidationError("Некорректное количество.") from exc
        if qty <= 0:
            continue

        custom_key = _normalized_custom_key(raw.get("custom_key")) if is_unlisted else ""
        identity = ("custom", custom_key) if is_unlisted else ("service", service_id)
        if identity in seen_identities:
            raise ValidationError("Одна и та же услуга не может быть указана дважды.")
        seen_identities.add(identity)

        service = None
        service_name = custom_name
        catalog_meta: dict[str, Any] = {}
        if not is_unlisted:
            service = BillingService.objects.filter(pk=service_id, is_active=True).first()
            if not service:
                raise ValidationError("Услуга не найдена.")
            catalog_meta = allowed[service_id]
            service_name = str(catalog_meta.get("name") or service.name or "")[:255]

        unit = str(
            raw.get("unit")
            or catalog_meta.get("unit")
            or getattr(service, "unit", "")
            or "шт"
        ).strip()[:32]
        if not unit:
            raise ValidationError("Укажите единицу измерения услуги.")
        comment = str(raw.get("comment") or "")[:255]
        planned_qty = _optional_decimal(raw.get("planned_quantity"))
        auto_qty = _optional_decimal(raw.get("auto_quantity"))
        discrepancy_reason = str(raw.get("discrepancy_reason") or "").strip()
        source = (
            UNLISTED_SERVICE_SOURCE
            if is_unlisted
            else str(raw.get("source") or "warehouse_manual").strip()[:64]
        )
        if is_unlisted and len(custom_name) < 3:
            raise ValidationError("Укажите понятное название новой услуги.")
        if is_unlisted and not discrepancy_reason:
            raise ValidationError("Опишите, какая услуга выполнена и почему её нет в списке.")
        if source == "processing_manual_adjustment" and not discrepancy_reason:
            raise ValidationError("Укажите причину ручной корректировки количества услуги.")
        if (
            order_type in {
                WarehouseServiceFact.ORDER_RECEIVING,
                WarehouseServiceFact.ORDER_PROCESSING,
                WarehouseServiceFact.ORDER_PACKING,
                WarehouseServiceFact.ORDER_SHIPPING,
                BillingApplication.TYPE_LOGISTICS,
                BillingApplication.TYPE_OTHER,
                WarehouseServiceFact.ORDER_FBS,
            }
            and planned_qty is not None
            and planned_qty != qty
            and not discrepancy_reason
        ):
            raise ValidationError("Укажите причину расхождения планового и фактического количества услуги.")
        warehouse_label = str(raw.get("warehouse_label") or "").strip()[:255]
        performed_at = _optional_datetime(raw.get("performed_at")) or timezone.now()
        metadata = dict(raw.get("metadata")) if isinstance(raw.get("metadata"), dict) else None
        if is_unlisted:
            metadata = metadata or {}
            metadata.update(
                {
                    UNLISTED_SERVICE_METADATA_KEY: True,
                    "custom_key": custom_key,
                    "original_custom_name": custom_name,
                }
            )
        if auto_qty is not None:
            metadata = metadata or {}
            metadata["auto_quantity"] = str(auto_qty)
        cleaned.append(
            {
                "identity": identity,
                "service": service,
                "service_name": service_name,
                "is_unlisted": is_unlisted,
                "custom_key": custom_key,
                "quantity": qty,
                "unit": unit,
                "comment": comment,
                "planned_quantity": planned_qty,
                "discrepancy_reason": discrepancy_reason,
                "source": source,
                "is_manual": source not in {"processing_auto", SHIPPING_PICK_AUTO_SOURCE},
                "warehouse_label": warehouse_label,
                "performed_at": performed_at,
                "metadata": metadata,
            }
        )

    existing_facts = list(
        WarehouseServiceFact.objects.filter(
            client=client,
            order_type=order_type,
            order_id=order_id,
        ).select_related("application", "charge")
    )
    existing_by_identity = {_fact_identity(fact): fact for fact in existing_facts}
    for fact in existing_facts:
        if fact.application_id and not _application_matches_warehouse_order(
            fact.application,
            client=client,
            order_type=order_type,
            order_id=order_id,
        ):
            raise ValidationError(
                "Факт связан с финансовой заявкой другого процесса. "
                "Редактирование заблокировано до отдельной сверки."
            )
    incoming_identities = {row["identity"] for row in cleaned}

    # Факт, который уже попал в отправленный/оплаченный счёт, — финансовая
    # история. Его нельзя тихо переписать или отменить из складского интерфейса.
    from .services import BillingWorkflowService

    def _locked(fact: WarehouseServiceFact) -> bool:
        return bool(fact.charge_id and BillingWorkflowService.charge_locked_by_sent_invoice(fact.charge))

    for identity, fact in existing_by_identity.items():
        manager_reviewed_unlisted = (
            identity[0] == "custom"
            and fact.status in {
                WarehouseServiceFact.STATUS_APPROVED,
                WarehouseServiceFact.STATUS_CHARGED,
            }
        )
        if identity not in incoming_identities and (_locked(fact) or manager_reviewed_unlisted):
            if manager_reviewed_unlisted:
                raise ValidationError(
                    "Услуга уже проверена менеджером. Отменить её можно только в биллинге менеджера."
                )
            raise ValidationError("Нельзя отменить факт, уже попавший в отправленный или оплаченный счёт.")

    created: list[WarehouseServiceFact] = []
    app = _compatible_billing_application(
        client=client,
        order_type=order_type,
        order_id=order_id,
    )
    for row in cleaned:
        identity = row["identity"]
        service = row["service"]
        qty = row["quantity"]
        unit = row["unit"]
        comment = row["comment"]
        planned_qty = row["planned_quantity"]
        discrepancy_reason = row["discrepancy_reason"]
        source = row["source"]
        warehouse_label = row["warehouse_label"]
        performed_at = row["performed_at"]
        metadata = row["metadata"]
        fact = existing_by_identity.get(identity)
        if row["is_unlisted"]:
            source_key = _unlisted_source_key(
                client_id=client.pk,
                order_type=order_type,
                order_id=order_id,
                custom_key=row["custom_key"],
            )
        else:
            source_key = f"warehouse_fact:{order_type}:{order_id}:{service.code}"
        if metadata is None:
            metadata = dict(fact.metadata) if fact and isinstance(fact.metadata, dict) else {}
        elif fact and row["is_unlisted"] and isinstance(fact.metadata, dict):
            preserved_metadata = dict(fact.metadata)
            preserved_metadata.update(metadata)
            metadata = preserved_metadata
        if (
            fact
            and row["is_unlisted"]
            and fact.status in {
                WarehouseServiceFact.STATUS_APPROVED,
                WarehouseServiceFact.STATUS_CHARGED,
            }
        ):
            original_name = str((fact.metadata or {}).get("original_custom_name") or "").strip()
            if any(
                (
                    original_name != row["service_name"],
                    fact.quantity != qty,
                    fact.unit != unit,
                    fact.comment != comment,
                    fact.planned_quantity != planned_qty,
                    fact.discrepancy_reason != discrepancy_reason,
                    fact.performed_at != performed_at,
                )
            ):
                raise ValidationError(
                    "Услуга уже проверена менеджером. Изменение доступно в биллинге менеджера."
                )
            created.append(fact)
            continue
        if fact and _locked(fact) and any(
            (
                fact.quantity != qty,
                fact.unit != unit,
                fact.comment != comment,
                fact.planned_quantity != planned_qty,
                fact.discrepancy_reason != discrepancy_reason,
                fact.performed_at != performed_at,
                fact.metadata != metadata,
            )
        ):
            raise ValidationError("Нельзя изменить факт, уже попавший в отправленный или оплаченный счёт.")
        defaults = {
            "service": service,
            "service_name_snapshot": row["service_name"],
            "planned_quantity": planned_qty,
            "quantity": qty,
            "unit": unit,
            "comment": comment,
            "discrepancy_reason": discrepancy_reason,
            "status": (
                WarehouseServiceFact.STATUS_WAITING_MANAGER_REVIEW
                if row["is_unlisted"]
                else WarehouseServiceFact.STATUS_SENT_TO_BILLING
            ),
            "source": source,
            "is_manual": row["is_manual"],
            "warehouse_label": warehouse_label,
            "performed_at": performed_at,
            "metadata": metadata,
            "reported_by": user if getattr(user, "is_authenticated", False) else None,
            "application": app,
            "source_key": source_key,
        }
        if fact:
            for field, value in defaults.items():
                setattr(fact, field, value)
            fact.save(update_fields=[*defaults.keys(), "updated_at"])
        else:
            fact = WarehouseServiceFact.objects.create(
                client=client,
                order_type=order_type,
                order_id=order_id,
                **defaults,
            )
        created.append(fact)

    # Не удаляем отсутствующие строки: они остаются в журнале с понятной причиной.
    for identity, fact in existing_by_identity.items():
        if identity in incoming_identities or fact.status == WarehouseServiceFact.STATUS_CANCELLED:
            continue
        fact.status = WarehouseServiceFact.STATUS_CANCELLED
        fact.discrepancy_reason = fact.discrepancy_reason or "Факт отозван складом"
        fact.save(update_fields=["status", "discrepancy_reason", "updated_at"])
        if fact.charge_id and not BillingWorkflowService.charge_locked_by_sent_invoice(fact.charge):
            BillingWorkflowService.ensure_charge_editable(fact.charge, user=user)
            BillingWorkflowService.exclude_charge(
                fact.charge,
                reason=fact.charge.EXCLUDE_NOT_PERFORMED,
                comment="Факт услуги отозван складом.",
                user=user,
            )
    return created


@transaction.atomic
def review_unlisted_warehouse_fact(
    *,
    fact_id: int,
    action: str,
    user=None,
    service_id: int | None = None,
    quantity: Any = None,
    unit: str = "",
    comment: str = "",
    manager_comment: str = "",
) -> WarehouseServiceFact:
    """Проверить услугу вне списка без автоматического начисления."""
    fact = (
        WarehouseServiceFact.objects.select_for_update(of=("self",))
        .select_related("service", "charge", "client")
        .filter(pk=fact_id)
        .first()
    )
    if not fact:
        raise ValidationError("Факт услуги не найден.")
    metadata = dict(fact.metadata) if isinstance(fact.metadata, dict) else {}
    if not metadata.get(UNLISTED_SERVICE_METADATA_KEY):
        raise ValidationError("Проверка доступна только для услуги вне согласованного списка.")
    if fact.charge_id:
        raise ValidationError("По услуге уже создано начисление. Используйте корректировку биллинга.")

    action = str(action or "").strip().lower()
    if action not in {"approve", "clarify", "cancel"}:
        raise ValidationError("Неизвестное действие проверки.")
    try:
        reviewed_quantity = Decimal(str(quantity if quantity not in {None, ""} else fact.quantity))
    except Exception as exc:
        raise ValidationError("Некорректное количество.") from exc
    if reviewed_quantity <= 0:
        raise ValidationError("Количество должно быть больше нуля.")
    reviewed_unit = str(unit or fact.unit or "шт").strip()[:32]
    if not reviewed_unit:
        raise ValidationError("Укажите единицу измерения услуги.")
    reviewed_comment = str(comment if comment is not None else fact.comment or "")[:255]
    manager_comment = str(manager_comment or "").strip()[:255]
    if action == "clarify" and not manager_comment:
        raise ValidationError("Укажите, что нужно уточнить складу.")

    reviewer_name = ""
    if getattr(user, "is_authenticated", False):
        reviewer_name = user.get_full_name().strip() or user.get_username() or str(user)
    review_entry = {
        "action": action,
        "reviewer_id": getattr(user, "pk", None),
        "reviewer_name": reviewer_name,
        "reviewed_at": timezone.now().isoformat(),
        "comment": manager_comment,
    }
    history = list(metadata.get("review_history") or [])[-19:]
    history.append(review_entry)
    metadata["manager_review"] = review_entry
    metadata["review_history"] = history

    fact.quantity = reviewed_quantity
    fact.unit = reviewed_unit
    fact.comment = reviewed_comment
    fact.metadata = metadata
    update_fields = ["quantity", "unit", "comment", "metadata"]

    if action == "approve":
        try:
            service_pk = int(service_id or 0)
        except (TypeError, ValueError):
            service_pk = 0
        service = BillingService.objects.filter(pk=service_pk, is_active=True).first()
        if not service:
            raise ValidationError("Выберите действующую услугу для сопоставления.")
        duplicate = WarehouseServiceFact.objects.filter(
            client_id=fact.client_id,
            order_type=fact.order_type,
            order_id=fact.order_id,
            service_id=service.pk,
        ).exclude(pk=fact.pk).exists()
        if duplicate:
            raise ValidationError("Эта услуга уже указана в заявке. Объедините количество в существующей строке.")
        fact.service = service
        fact.service_name_snapshot = service.name[:255]
        fact.status = WarehouseServiceFact.STATUS_APPROVED
        fact.source_key = f"warehouse_fact:{fact.order_type}:{fact.order_id}:{service.code}"
        update_fields.extend(["service", "service_name_snapshot", "status", "source_key"])
    elif action == "clarify":
        fact.service = None
        fact.service_name_snapshot = str(
            metadata.get("original_custom_name") or fact.service_name_snapshot or ""
        )[:255]
        fact.status = WarehouseServiceFact.STATUS_NEEDS_CLARIFICATION
        fact.source_key = _unlisted_source_key(
            client_id=fact.client_id,
            order_type=fact.order_type,
            order_id=fact.order_id,
            custom_key=str(metadata.get("custom_key") or fact.pk),
        )
        update_fields.extend(["service", "service_name_snapshot", "status", "source_key"])
    else:
        fact.status = WarehouseServiceFact.STATUS_CANCELLED
        if manager_comment:
            fact.discrepancy_reason = manager_comment
            update_fields.append("discrepancy_reason")
        update_fields.append("status")

    fact.save(update_fields=[*dict.fromkeys(update_fields), "updated_at"])
    return fact


@transaction.atomic
def sync_processing_facts_to_billing(
    *,
    client: Agency,
    order_id: str,
    completed_at=None,
    user=None,
    source_payload: dict[str, Any] | None = None,
) -> tuple[BillingApplication, list[WarehouseServiceFact]]:
    """Передать подтверждённые факты обработки в существующий реестр биллинга.

    Метод создаёт или обновляет только BillingApplication и связывает с ней факты.
    Начисления и счета здесь не рассчитываются.
    """
    order_id = str(order_id or "").strip()
    if not client or not order_id:
        raise ValidationError("Укажите клиента и номер заявки.")
    required_facts = require_completion_facts(
        client=client,
        order_type=WarehouseServiceFact.ORDER_PROCESSING,
        order_id=order_id,
    )
    facts = list(
        WarehouseServiceFact.objects.select_for_update(of=("self",))
        .filter(pk__in=[fact.pk for fact in required_facts])
        .select_related("service", "reported_by")
        .order_by("id")
    )

    from employees.models import Employee

    from .models import BillingStaffNotification
    from .services import BillingWorkflowService
    from .staff_notifications import notify_client_manager

    manager = None
    manager_user_id = getattr(client, "mened_user_id", None)
    if manager_user_id:
        manager = Employee.objects.filter(user_id=manager_user_id).order_by("id").first()
    completed_at = completed_at or timezone.now()
    existing_application = _compatible_billing_application(
        client=client,
        order_type=WarehouseServiceFact.ORDER_PROCESSING,
        order_id=order_id,
    )
    created_at_source = getattr(existing_application, "created_at_source", None)
    if created_at_source is None:
        from audit.models import OrderAuditEntry

        created_at_source = (
            OrderAuditEntry.objects.filter(
                agency=client,
                order_id=order_id,
                order_type__in=compatible_warehouse_order_types(
                    WarehouseServiceFact.ORDER_PROCESSING
                ),
            )
            .order_by("created_at", "id")
            .values_list("created_at", flat=True)
            .first()
        )
    created_at_source = created_at_source or completed_at
    facts_snapshot = facts_payload(facts)
    application = BillingWorkflowService.sync_application_from_source(
        application_type=BillingApplication.TYPE_PROCESSING,
        application_id=order_id,
        client=client,
        legal_entity=client,
        manager=manager,
        operational_status="done",
        operational_status_label="Заявка на обработку закрыта",
        created_at_source=created_at_source,
        source_payload={
            **(source_payload or {}),
            "source": "processing_close",
            "order_id": order_id,
            "completed_at": completed_at.isoformat(),
            "service_facts": facts_snapshot,
        },
        user=user,
    )
    if not application.is_operations_completed:
        BillingWorkflowService.mark_operations_completed(
            application,
            completed_at=completed_at,
            user=user,
        )
    WarehouseServiceFact.objects.filter(pk__in=[fact.pk for fact in facts]).update(application=application)
    for fact in facts:
        fact.application = application

    notify_client_manager(
        client,
        kind=BillingStaffNotification.KIND_OTHER,
        title=f"Услуги обработки {order_id}",
        message=f"Склад передал {len(facts)} факт(а) услуг. Проверьте начисления по обработке.",
        link_url=f"/team-manager/billing/applications/{application.pk}/",
        source_key=f"warehouse-facts:processing:{client.pk}:{order_id}:{application.pk}",
        actor=user,
    )
    return application, facts


def logistics_trip_clients(trip) -> list[Agency]:
    """Клиенты рейса, для каждого из которых нужен отдельный набор услуг."""
    from logistics.models import LogisticsTrip

    if trip.trip_kind == LogisticsTrip.KIND_EXTERNAL:
        details = getattr(trip, "external_details", None)
        return [details.client] if details and details.client_id else []
    client_ids = list(
        trip.orders.exclude(shipping_order__agency_id__isnull=True)
        .order_by("shipping_order__agency_id")
        .values_list("shipping_order__agency_id", flat=True)
        .distinct()
    )
    clients = Agency.objects.in_bulk(client_ids)
    return [clients[client_id] for client_id in client_ids if client_id in clients]


def require_logistics_trip_completion_facts(trip) -> dict[int, list[WarehouseServiceFact]]:
    """Не разрешать завершение, пока услуги не указаны по каждому клиенту рейса."""
    clients = logistics_trip_clients(trip)
    if not clients:
        raise ValidationError("В рейсе не найден клиент для передачи услуг в биллинг.")
    facts_by_client: dict[int, list[WarehouseServiceFact]] = {}
    missing = []
    for client in clients:
        facts = completion_facts(
            client=client,
            order_type=BillingApplication.TYPE_LOGISTICS,
            order_id=trip.number,
        )
        if facts:
            facts_by_client[client.pk] = facts
        else:
            missing.append(str(client))
    if missing:
        raise ValidationError(
            "Перед завершением рейса укажите фактически оказанные услуги для клиентов: "
            + ", ".join(missing)
            + "."
        )
    return facts_by_client


@transaction.atomic
def sync_logistics_trip_facts_to_billing(*, trip, user=None) -> list[BillingApplication]:
    """Передать услуги завершенного рейса менеджерам без автоматического расчета."""
    from employees.models import Employee
    from logistics.models import LogisticsTrip

    from .models import BillingStaffNotification
    from .services import BillingWorkflowService
    from .staff_notifications import notify_client_manager

    facts_by_client = require_logistics_trip_completion_facts(trip)
    clients = {client.pk: client for client in logistics_trip_clients(trip)}
    completed_at = trip.closed_at or timezone.now()
    applications = []
    for client_id, required_facts in facts_by_client.items():
        client = clients[client_id]
        facts = list(
            WarehouseServiceFact.objects.select_for_update(of=("self",))
            .filter(pk__in=[fact.pk for fact in required_facts])
            .select_related("service", "reported_by")
            .order_by("id")
        )
        manager = None
        manager_user_id = getattr(client, "mened_user_id", None)
        if manager_user_id:
            manager = Employee.objects.filter(user_id=manager_user_id).order_by("id").first()

        order_numbers = []
        expected_boxes = 0
        if trip.trip_kind != LogisticsTrip.KIND_EXTERNAL:
            client_orders = trip.orders.filter(shipping_order__agency_id=client_id).select_related(
                "shipping_order"
            )
            for link in client_orders:
                order_numbers.append(link.shipping_order.number)
                expected_boxes += int(link.shipping_order.expected_boxes or 0)
        details = getattr(trip, "external_details", None)
        route = ""
        if details:
            route = f"{details.pickup_address} -> {details.delivery_address}"

        application = BillingWorkflowService.sync_application_from_source(
            application_type=BillingApplication.TYPE_LOGISTICS,
            application_id=trip.number,
            client=client,
            legal_entity=client,
            manager=manager,
            operational_status=trip.status,
            operational_status_label=trip.get_status_display(),
            created_at_source=trip.created_at,
            source_payload={
                "source": "logistics_trip_close",
                "trip_id": trip.pk,
                "trip_number": trip.number,
                "trip_kind": trip.trip_kind,
                "route": route,
                "order_numbers": order_numbers,
                "order_count": len(order_numbers) or 1,
                "expected_boxes": expected_boxes,
                "completed_at": completed_at.isoformat(),
                "service_facts": facts_payload(facts),
            },
            user=user,
        )
        if not application.is_operations_completed:
            BillingWorkflowService.mark_operations_completed(
                application,
                completed_at=completed_at,
                user=user,
            )
        WarehouseServiceFact.objects.filter(pk__in=[fact.pk for fact in facts]).update(
            application=application
        )
        notify_client_manager(
            client,
            kind=BillingStaffNotification.KIND_OTHER,
            title=f"Услуги рейса {trip.number}",
            message=f"Передано {len(facts)} факт(а) услуг рейса. Проверьте начисления.",
            link_url=f"/team-manager/billing/applications/{application.pk}/",
            source_key=f"warehouse-facts:logistics:{client.pk}:{trip.number}:{application.pk}",
            actor=user,
        )
        applications.append(application)
    return applications


@transaction.atomic
def sync_receiving_facts_to_billing(
    *,
    client: Agency,
    order_id: str,
    completed_at=None,
    user=None,
    source_payload: dict[str, Any] | None = None,
) -> tuple[BillingApplication, list[WarehouseServiceFact]]:
    """Зафиксировать завершенную приемку в биллинге без расчета цены."""
    order_id = str(order_id or "").strip()
    if not client or not order_id:
        raise ValidationError("Укажите клиента и номер заявки.")
    facts = require_completion_facts(
        client=client,
        order_type=WarehouseServiceFact.ORDER_RECEIVING,
        order_id=order_id,
    )

    from employees.models import Employee

    from .models import BillingStaffNotification
    from .services import BillingWorkflowService
    from .staff_notifications import notify_client_manager

    manager = None
    manager_user_id = getattr(client, "mened_user_id", None)
    if manager_user_id:
        manager = Employee.objects.filter(user_id=manager_user_id).order_by("id").first()
    completed_at = completed_at or timezone.now()
    existing_application = _compatible_billing_application(
        client=client,
        order_type=WarehouseServiceFact.ORDER_RECEIVING,
        order_id=order_id,
    )
    created_at_source = getattr(existing_application, "created_at_source", None)
    if created_at_source is None:
        from audit.models import OrderAuditEntry

        created_at_source = (
            OrderAuditEntry.objects.filter(
                agency=client,
                order_id=order_id,
                order_type=WarehouseServiceFact.ORDER_RECEIVING,
            )
            .order_by("created_at", "id")
            .values_list("created_at", flat=True)
            .first()
        )
    created_at_source = created_at_source or completed_at
    facts_snapshot = facts_payload(facts)
    application = BillingWorkflowService.sync_application_from_source(
        application_type=BillingApplication.TYPE_RECEIVING,
        application_id=order_id,
        client=client,
        legal_entity=client,
        manager=manager,
        operational_status="done",
        operational_status_label="Приемка завершена складом",
        created_at_source=created_at_source,
        source_payload={
            **(source_payload or {}),
            "source": "receiving_close",
            "order_id": order_id,
            "completed_at": completed_at.isoformat(),
            "service_facts": facts_snapshot,
        },
        user=user,
    )
    if not application.is_operations_completed:
        BillingWorkflowService.mark_operations_completed(
            application,
            completed_at=completed_at,
            user=user,
        )
    WarehouseServiceFact.objects.filter(pk__in=[fact.pk for fact in facts]).update(application=application)
    for fact in facts:
        fact.application = application

    notify_client_manager(
        client,
        kind=BillingStaffNotification.KIND_OTHER,
        title=f"Услуги приемки {order_id}",
        message=f"Склад передал {len(facts)} факт(а) услуг. Проверьте начисления по приемке.",
        link_url=f"/team-manager/billing/applications/{application.pk}/",
        source_key=f"warehouse-facts:receiving:{client.pk}:{order_id}:{application.pk}",
        actor=user,
    )
    return application, facts


@transaction.atomic
def sync_shipping_facts_to_billing(
    *,
    client: Agency,
    order_id: str,
    completed_at=None,
    user=None,
    source_payload: dict[str, Any] | None = None,
) -> tuple[BillingApplication, list[WarehouseServiceFact]]:
    """Зафиксировать завершенную складскую погрузку без расчета цены."""
    order_id = str(order_id or "").strip()
    if not client or not order_id:
        raise ValidationError("Укажите клиента и номер заявки.")
    required_facts = require_completion_facts(
        client=client,
        order_type=WarehouseServiceFact.ORDER_SHIPPING,
        order_id=order_id,
    )
    facts = list(
        WarehouseServiceFact.objects.select_for_update(of=("self",))
        .filter(pk__in=[fact.pk for fact in required_facts])
        .select_related("service", "reported_by")
        .order_by("id")
    )

    from employees.models import Employee
    from shipping.models import ShippingOrder

    from .models import BillingStaffNotification
    from .services import BillingWorkflowService
    from .staff_notifications import notify_client_manager

    shipping_order = (
        ShippingOrder.objects.filter(number=order_id, agency=client)
        .only("created_at")
        .first()
    )
    if shipping_order is None:
        raise ValidationError("Заявка клиента не найдена.")

    manager = None
    manager_user_id = getattr(client, "mened_user_id", None)
    if manager_user_id:
        manager = Employee.objects.filter(user_id=manager_user_id).order_by("id").first()
    completed_at = completed_at or timezone.now()
    existing_application = _compatible_billing_application(
        client=client,
        order_type=WarehouseServiceFact.ORDER_SHIPPING,
        order_id=order_id,
    )
    created_at_source = getattr(existing_application, "created_at_source", None)
    created_at_source = created_at_source or shipping_order.created_at or completed_at
    facts_snapshot = facts_payload(facts)
    application = BillingWorkflowService.sync_application_from_source(
        application_type=BillingApplication.TYPE_SHIPPING,
        application_id=order_id,
        client=client,
        legal_entity=client,
        manager=manager,
        operational_status="loaded",
        operational_status_label="Погрузка завершена складом",
        created_at_source=created_at_source,
        source_payload={
            **(source_payload or {}),
            "source": "shipping_loading_close",
            "order_id": order_id,
            "completed_at": completed_at.isoformat(),
            "service_facts": facts_snapshot,
        },
        user=user,
    )
    if not application.is_operations_completed:
        BillingWorkflowService.mark_operations_completed(
            application,
            completed_at=completed_at,
            user=user,
        )
    WarehouseServiceFact.objects.filter(pk__in=[fact.pk for fact in facts]).update(application=application)
    for fact in facts:
        fact.application = application

    notify_client_manager(
        client,
        kind=BillingStaffNotification.KIND_OTHER,
        title=f"Услуги отгрузки {order_id}",
        message=f"Склад передал {len(facts)} факт(а) услуг. Проверьте начисления по отгрузке.",
        link_url=f"/team-manager/billing/applications/{application.pk}/",
        source_key=f"warehouse-facts:shipping:{client.pk}:{order_id}:{application.pk}",
        actor=user,
    )
    return application, facts


@transaction.atomic
def sync_other_request_facts_to_billing(
    *,
    request_obj,
    user=None,
) -> tuple[BillingApplication, list[WarehouseServiceFact]]:
    """Передать фактически оказанные услуги другой заявки без авторасчета."""
    client = getattr(request_obj, "agency", None)
    order_id = str(getattr(request_obj, "public_number", "") or "").strip()
    if not client or not order_id:
        raise ValidationError("У другой заявки не указан клиент или номер.")
    required_facts = require_completion_facts(
        client=client,
        order_type=BillingApplication.TYPE_OTHER,
        order_id=order_id,
    )
    facts = list(
        WarehouseServiceFact.objects.select_for_update(of=("self",))
        .filter(pk__in=[fact.pk for fact in required_facts])
        .select_related("service", "reported_by")
        .order_by("id")
    )

    from .models import BillingStaffNotification
    from .services import BillingWorkflowService
    from .staff_notifications import notify_client_manager

    completed_at = getattr(request_obj, "closed_at", None) or timezone.now()
    manager = getattr(request_obj, "manager", None)
    application = BillingWorkflowService.sync_application_from_source(
        application_type=BillingApplication.TYPE_OTHER,
        application_id=order_id,
        client=client,
        legal_entity=client,
        manager=manager,
        operational_status=getattr(request_obj, "status", "closed"),
        operational_status_label=getattr(request_obj, "status_label", "Другая заявка закрыта"),
        created_at_source=getattr(request_obj, "created_at", None) or completed_at,
        source_payload={
            "source": "other_request_close",
            "order_id": order_id,
            "title": getattr(request_obj, "display_title", "") or "",
            "category": getattr(request_obj, "category_code", "") or "",
            "department": getattr(request_obj, "department", "") or "",
            "result_outcome": getattr(request_obj, "result_outcome", "") or "",
            "result_comment": getattr(request_obj, "result_comment", "") or "",
            "completed_at": completed_at.isoformat(),
            "service_facts": facts_payload(facts),
        },
        user=user,
    )
    if not application.is_operations_completed:
        BillingWorkflowService.mark_operations_completed(
            application,
            completed_at=completed_at,
            user=user,
        )
    WarehouseServiceFact.objects.filter(pk__in=[fact.pk for fact in facts]).update(
        application=application
    )
    for fact in facts:
        fact.application = application

    request_obj.billing_status = str(getattr(application, "billing_status", "") or "")
    update_fields = ["billing_status", "updated_at"]
    if not getattr(request_obj, "billing_service_code", ""):
        request_obj.billing_service_code = "extra_warehouse_operation"
        update_fields.append("billing_service_code")
    request_obj.save(update_fields=update_fields)

    notify_client_manager(
        client,
        kind=BillingStaffNotification.KIND_OTHER,
        title=f"Услуги другой заявки {order_id}",
        message=f"Склад передал {len(facts)} факт(а) услуг. Проверьте начисления.",
        link_url=f"/team-manager/billing/applications/{application.pk}/",
        source_key=f"warehouse-facts:other:{client.pk}:{order_id}:{application.pk}",
        actor=user,
    )
    return application, facts


def candidates_from_warehouse_facts(application: BillingApplication) -> list[ChargeCandidate] | None:
    """Если кладовщик указал факты — они становятся источником начислений."""
    all_facts = list(
        WarehouseServiceFact.objects.filter(
            client_id=application.client_id,
            order_id=str(application.application_id),
            order_type__in=compatible_warehouse_order_types(application.application_type),
        )
        .filter(
            Q(application__isnull=True) | Q(application=application),
        ).select_related("service")
    )
    if not all_facts:
        return None
    eligible_statuses = {
        WarehouseServiceFact.STATUS_SENT_TO_BILLING,
        WarehouseServiceFact.STATUS_APPROVED,
        WarehouseServiceFact.STATUS_CHARGED,
    }
    facts = [fact for fact in all_facts if fact.status in eligible_statuses]
    lines: list[ChargeCandidate] = []
    for fact in facts:
        if not fact.service_id:
            continue
        code = getattr(fact.service, "code", "") or ""
        if not code:
            continue
        lines.append(
            ChargeCandidate(
                service_code=code,
                quantity=Decimal(str(fact.quantity)),
                operation_type=fact.order_type,
                operation_id=fact.order_id,
                source_key=fact.source_key or f"warehouse_fact:{fact.order_type}:{fact.order_id}:{code}",
                comment=fact.comment or f"Услуга склада по заявке {fact.order_id}",
                performed_at=fact.performed_at,
            )
        )
    return lines


def exclude_stale_warehouse_fact_charges(
    application: BillingApplication,
    *,
    source_keys: set[str],
    user=None,
) -> int:
    """Исключить открытые начисления по отозванным/неподходящим фактам.

    Удаление начисления здесь недопустимо: менеджер должен видеть причину, а
    отправленные финансовые документы остаются неизменяемыми.
    """
    from .services import BillingWorkflowService

    stale = application.charges.filter(
        source_key__startswith="warehouse_fact:",
        is_excluded=False,
    ).exclude(source_key__in=source_keys)
    excluded = 0
    for charge in stale.select_related("service"):
        if BillingWorkflowService.charge_locked_by_sent_invoice(charge):
            continue
        try:
            BillingWorkflowService.ensure_charge_editable(charge, user=user)
            BillingWorkflowService.exclude_charge(
                charge,
                reason=charge.EXCLUDE_NOT_PERFORMED,
                comment="Факт склада отменён или ожидает уточнения.",
                user=user,
            )
            excluded += 1
        except ValidationError:
            # Отправленный акт — самостоятельный финансовый документ: его
            # корректируют только отдельной корректировкой, а не автопересчётом.
            continue
    return excluded


def link_facts_to_charges(application: BillingApplication) -> int:
    """Привязать факты к созданным начислениям по source_key."""
    linked = 0
    charges = {
        str(c.source_key): c
        for c in application.charges.all()
        if c.source_key
    }
    for fact in (
        WarehouseServiceFact.objects.filter(
            client_id=application.client_id,
            order_id=str(application.application_id),
            order_type__in=compatible_warehouse_order_types(application.application_type),
        )
        .filter(Q(application__isnull=True) | Q(application=application))
        .select_related("service")
    ):
        if not fact.service_id:
            continue
        key = fact.source_key or f"warehouse_fact:{fact.order_type}:{fact.order_id}:{fact.service.code}"
        charge = charges.get(key)
        update_fields = []
        if fact.application_id != application.id:
            fact.application = application
            update_fields.append("application")
        if charge and fact.charge_id != charge.id:
            fact.charge = charge
            update_fields.append("charge")
        if charge and fact.status != WarehouseServiceFact.STATUS_CHARGED:
            fact.status = WarehouseServiceFact.STATUS_CHARGED
            update_fields.append("status")
        if update_fields:
            update_fields.append("updated_at")
            fact.save(update_fields=update_fields)
            linked += 1
    return linked


def section_codes_for_process(process_type: str) -> set[str]:
    order_type = normalize_order_type(process_type)
    return set(PROCESS_SECTIONS.get(order_type) or set())


def category_codes_from_sections(sections: set[str]) -> set[str]:
    codes = set()
    for section in sections:
        mapped = SECTION_TO_CATEGORY.get(section)
        if mapped:
            codes.add(mapped[0])
    return codes
