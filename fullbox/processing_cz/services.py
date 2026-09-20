from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import QueryDict
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.access import get_request_role
from fullbox.container_codes import issue_container_code, issue_pallet_code
from marking.codes import (
    marking_code_identity,
    marking_code_variants,
    normalize_marking_code as canonicalize_marking_code,
)
from marking.models import MarkingCode
from orders.web_ui import _flow_closed_from_entries
from orders.services import ReceivingWorkflowService
from processing_app.models import ProcessingFlowSession
from processing_app.services import ProcessingWorkflowService
from processing_app.web_ui import (
    ProcessingFlowView,
    _parse_qty_value,
    _processing_card_sets,
    _processing_receiving_items,
    _processing_work_payload_from_entries,
)
from sku.models import Agency, SKU, SKUBarcode

from .models import ProcessingCzUnit


@dataclass
class ProcessingCzItem:
    sku_code: str
    name: str = ""
    size: str = ""
    barcode: str = ""
    barcodes: list[str] = field(default_factory=list)
    planned_qty: int = 0
    sku: SKU | None = None

    @property
    def key(self) -> str:
        return _item_key(self.sku_code, self.size)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "sku_code": self.sku_code,
            "name": self.name,
            "size": self.size,
            "barcode": self.barcode,
            "barcodes": list(self.barcodes),
            "planned_qty": self.planned_qty,
            "qty": self.planned_qty,
            "accepted_qty": 0,
            "brand": "",
            "color": "",
        }


@dataclass
class ProcessingCzOrderContext:
    order_id: str
    entries: list[OrderAuditEntry]
    latest: OrderAuditEntry | None
    agency: Agency | None
    payload: dict
    items: list[ProcessingCzItem]


@dataclass
class ProcessingCzScanResult:
    status: str
    error: str = ""
    unit: ProcessingCzUnit | None = None
    item_count: int = 0
    total_count: int = 0


@dataclass
class ProcessingCzDeleteResult:
    status: str
    error: str = ""
    unit: dict | None = None
    total_count: int = 0


@dataclass
class ProcessingCzDeleteBoxResult:
    status: str
    error: str = ""
    units: list[dict] = field(default_factory=list)
    total_count: int = 0


def normalize_marking_code(value: str | None) -> str:
    return canonicalize_marking_code(value)


def _item_key(sku_code: str, size: str) -> str:
    return f"{str(sku_code or '').strip().lower()}|{str(size or '').strip().lower()}"


def _flow_closed(entries: list[OrderAuditEntry]) -> bool:
    return _flow_closed_from_entries(entries)


def load_order_context(order_id: str) -> ProcessingCzOrderContext | None:
    order_key = str(order_id or "").strip()
    if not order_key:
        return None
    entries = list(
        OrderAuditEntry.objects.filter(order_id=order_key, order_type="processing")
        .select_related("agency", "user")
        .order_by("created_at", "id")
    )
    if not entries:
        return None
    latest = entries[-1]
    payload = _processing_work_payload_from_entries(entries)
    agency = latest.agency
    return ProcessingCzOrderContext(
        order_id=order_key,
        entries=entries,
        latest=latest,
        agency=agency,
        payload=payload,
        items=_build_items(agency, payload),
    )


def _build_items(agency: Agency | None, payload: dict) -> list[ProcessingCzItem]:
    processed_cards, placed_cards = _processing_card_sets(payload)
    ready_cards = processed_cards - placed_cards if processed_cards else set()
    raw_items = _processing_receiving_items(
        payload,
        agency.id if agency else None,
        ready_cards if ready_cards else None,
    )
    rows: dict[tuple[str, str], ProcessingCzItem] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        sku_code = str(raw.get("sku_code") or raw.get("sku") or "").strip()
        if not sku_code:
            continue
        size = str(raw.get("size") or "").strip()
        key = (sku_code.lower(), size.lower())
        row = rows.setdefault(
            key,
            ProcessingCzItem(
                sku_code=sku_code,
                name=str(raw.get("name") or "").strip(),
                size=size,
                barcode=str(raw.get("barcode") or "").strip(),
            ),
        )
        qty = _parse_qty_value(raw.get("actual_qty"))
        if qty is None:
            qty = _parse_qty_value(raw.get("qty")) or 0
        row.planned_qty += max(int(qty or 0), 0)
        if not row.name:
            row.name = str(raw.get("name") or "").strip()
        if not row.barcode:
            row.barcode = str(raw.get("barcode") or "").strip()

    if not rows:
        return []

    sku_codes = {item.sku_code for item in rows.values() if item.sku_code}
    sku_qs = SKU.objects.filter(sku_code__in=sku_codes, deleted=False)
    if agency:
        sku_qs = sku_qs.filter(agency=agency)
    sku_by_code = {str(sku.sku_code or "").strip().lower(): sku for sku in sku_qs}
    for item in rows.values():
        sku = sku_by_code.get(item.sku_code.lower())
        item.sku = sku
        if sku and not item.name:
            item.name = sku.name
        if not item.size and sku and sku.size:
            item.size = str(sku.size or "").strip()

    barcode_qs = SKUBarcode.objects.select_related("sku").filter(sku__sku_code__in=sku_codes, sku__deleted=False)
    if agency:
        barcode_qs = barcode_qs.filter(sku__agency=agency)
    for barcode in barcode_qs.order_by("sku__sku_code", "-is_primary", "id"):
        value = str(barcode.value or "").strip()
        if not value:
            continue
        sku_code = str(barcode.sku.sku_code or "").strip().lower()
        size = str(barcode.size or barcode.sku.size or "").strip().lower()
        candidates = [
            rows.get((sku_code, size)),
            rows.get((sku_code, "")),
        ]
        item = next((candidate for candidate in candidates if candidate is not None), None)
        if item is None:
            continue
        if value not in item.barcodes:
            item.barcodes.append(value)
        if not item.barcode:
            item.barcode = value

    for item in rows.values():
        if item.barcode and item.barcode not in item.barcodes:
            item.barcodes.insert(0, item.barcode)
    return list(rows.values())


def _catalog_items(context: ProcessingCzOrderContext) -> list[ProcessingCzItem]:
    if not context.agency:
        return []
    existing = {item.key for item in context.items}
    rows: dict[tuple[str, str], ProcessingCzItem] = {}
    barcode_qs = (
        SKUBarcode.objects.select_related("sku")
        .filter(sku__agency=context.agency, sku__deleted=False)
        .order_by("sku__sku_code", "-is_primary", "id")
    )
    for barcode in barcode_qs:
        sku = barcode.sku
        sku_code = str(sku.sku_code or "").strip()
        if not sku_code:
            continue
        size = str(barcode.size or sku.size or "").strip()
        key_text = _item_key(sku_code, size)
        if key_text in existing:
            continue
        row = rows.setdefault(
            (sku_code.lower(), size.lower()),
            ProcessingCzItem(
                sku_code=sku_code,
                name=sku.name or "",
                size=size,
                barcode=str(barcode.value or "").strip(),
                planned_qty=0,
                sku=sku,
            ),
        )
        value = str(barcode.value or "").strip()
        if value and value not in row.barcodes:
            row.barcodes.append(value)
        if barcode.is_primary and value:
            row.barcode = value
    for item in rows.values():
        if item.barcode and item.barcode not in item.barcodes:
            item.barcodes.insert(0, item.barcode)
    return list(rows.values())


def _unit_items(order_id: str, known_keys: set[str]) -> list[ProcessingCzItem]:
    rows: dict[tuple[str, str], ProcessingCzItem] = {}
    for unit in accepted_units(order_id):
        key_text = _item_key(unit.sku_code, unit.size)
        if key_text in known_keys:
            continue
        row = rows.setdefault(
            (unit.sku_code.lower(), unit.size.lower()),
            ProcessingCzItem(
                sku_code=unit.sku_code,
                name=unit.name,
                size=unit.size,
                barcode=unit.barcode,
                planned_qty=0,
                sku=unit.sku,
            ),
        )
        if unit.barcode and unit.barcode not in row.barcodes:
            row.barcodes.append(unit.barcode)
    return list(rows.values())


def _find_item_by_barcode(
    context: ProcessingCzOrderContext,
    barcode: str,
    *,
    sku_code: str = "",
    size: str = "",
) -> ProcessingCzItem | None:
    value = str(barcode or "").strip()
    sku_text = str(sku_code or "").strip()
    size_text = str(size or "").strip()
    if sku_text:
        matches = [item for item in context.items if item.sku_code.lower() == sku_text.lower()]
        exact = next((item for item in matches if not size_text or item.size.lower() == size_text.lower()), None)
        if exact:
            if value and value not in exact.barcodes:
                exact.barcodes.append(value)
            if value and not exact.barcode:
                exact.barcode = value
            return exact
    if not value:
        return None
    for item in context.items:
        if value == item.barcode or value in item.barcodes:
            return item
    if not context.agency:
        return None
    barcode_obj = (
        SKUBarcode.objects.select_related("sku")
        .filter(
            agency=context.agency,
            value=value,
            sku__deleted=False,
        )
        .first()
    )
    if not barcode_obj or not barcode_obj.sku:
        return None
    resolved_sku = str(barcode_obj.sku.sku_code or "").strip().lower()
    resolved_size = str(barcode_obj.size or barcode_obj.sku.size or "").strip().lower()
    matches = [item for item in context.items if item.sku_code.lower() == resolved_sku]
    exact = next((item for item in matches if item.size.lower() == resolved_size), None)
    if exact:
        return exact
    if len(matches) == 1:
        return matches[0]
    return ProcessingCzItem(
        sku_code=str(barcode_obj.sku.sku_code or "").strip(),
        name=barcode_obj.sku.name or "",
        size=str(barcode_obj.size or barcode_obj.sku.size or "").strip(),
        barcode=value,
        barcodes=[value],
        planned_qty=0,
        sku=barcode_obj.sku,
    )


def serialize_unit(unit: ProcessingCzUnit) -> dict:
    return {
        "id": unit.id,
        "sku_code": unit.sku_code,
        "name": unit.name,
        "size": unit.size,
        "barcode": unit.barcode,
        "marking_code": unit.marking_code,
        "box_code": unit.box_code,
        "pallet_code": unit.pallet_code,
        "accepted_at": unit.accepted_at.isoformat() if unit.accepted_at else "",
    }


def accepted_units(order_id: str):
    return ProcessingCzUnit.objects.filter(order_id=str(order_id or "").strip()).select_related("sku", "agency")


def _release_marking_code_for_unit(*, context: ProcessingCzOrderContext, unit: ProcessingCzUnit) -> None:
    mark = MarkingCode.objects.select_for_update().filter(
        code=unit.marking_code,
        order_type="processing",
        order_id=context.order_id,
    ).first()
    if not mark:
        return
    if mark.source == "scan":
        mark.delete()
        return
    mark.order_id = ""
    mark.box_barcode = ""
    mark.used_at = None
    mark.used_by = None
    mark.save(update_fields=["order_id", "box_barcode", "used_at", "used_by"])


@transaction.atomic
def delete_unit(*, context: ProcessingCzOrderContext, unit_id: int) -> ProcessingCzDeleteResult:
    if _flow_closed(context.entries):
        return ProcessingCzDeleteResult(status="closed", error="Раскоробовка уже закрыта.")
    unit = (
        ProcessingCzUnit.objects.select_for_update()
        .filter(order_id=context.order_id, id=unit_id)
        .first()
    )
    if unit is None:
        return ProcessingCzDeleteResult(status="not_found", error="ЧЗ не найден в этой раскоробовке.")
    unit_payload = serialize_unit(unit)
    _release_marking_code_for_unit(context=context, unit=unit)
    unit.delete()
    total_count = ProcessingCzUnit.objects.filter(order_id=context.order_id).count()
    return ProcessingCzDeleteResult(status="ok", unit=unit_payload, total_count=total_count)


@transaction.atomic
def delete_units_for_box(*, context: ProcessingCzOrderContext, box_code: str) -> ProcessingCzDeleteBoxResult:
    if _flow_closed(context.entries):
        return ProcessingCzDeleteBoxResult(status="closed", error="Раскоробовка уже закрыта.")
    normalized_box_code = str(box_code or "").strip()
    if not normalized_box_code:
        return ProcessingCzDeleteBoxResult(status="invalid", error="box_code_required")
    units = list(
        ProcessingCzUnit.objects.select_for_update()
        .filter(order_id=context.order_id, box_code=normalized_box_code)
        .order_by("accepted_at", "id")
    )
    unit_payloads = [serialize_unit(unit) for unit in units]
    for unit in units:
        _release_marking_code_for_unit(context=context, unit=unit)
        unit.delete()
    total_count = ProcessingCzUnit.objects.filter(order_id=context.order_id).count()
    return ProcessingCzDeleteBoxResult(status="ok", units=unit_payloads, total_count=total_count)


def context_payload(context: ProcessingCzOrderContext) -> dict:
    units = list(accepted_units(context.order_id).order_by("accepted_at", "id"))
    counts: dict[str, int] = {}
    for unit in units:
        counts[unit_key(unit)] = counts.get(unit_key(unit), 0) + 1
    known_keys = {item.key for item in context.items}
    display_source = list(context.items) + _unit_items(context.order_id, known_keys)
    items = []
    for item in display_source:
        item_payload = item.as_dict()
        item_payload["accepted_qty"] = counts.get(item.key, 0)
        items.append(item_payload)
    barcode_items_by_key = {item.key: item.as_dict() for item in context.items}
    for item in _catalog_items(context):
        barcode_items_by_key.setdefault(item.key, item.as_dict())
    return {
        "items": items,
        "barcode_items": list(barcode_items_by_key.values()),
        "catalog_items": list(barcode_items_by_key.values()),
        "marked_items": [],
        "cz_accepted_units": [serialize_unit(unit) for unit in units],
        "total_count": len(units),
    }


def unit_key(unit: ProcessingCzUnit) -> str:
    return _item_key(unit.sku_code, unit.size)


def issue_container_for_order(context: ProcessingCzOrderContext, *, kind: str, goods_type: str = "") -> dict:
    if not context.agency:
        return {"ok": False, "error": "client_not_found"}
    resolved_goods_type = str(goods_type or context.payload.get("goods_type") or "").strip().lower()
    if str(kind or "").strip().lower() == "pallet":
        return {
            "ok": True,
            **issue_pallet_code(
                agency=context.agency,
                goods_type=resolved_goods_type,
                order_type="processing",
                order_id=context.order_id,
            ),
        }
    return {"ok": True, **issue_container_code(agency=context.agency, goods_type=resolved_goods_type)}


def _receiving_cz_has_code(code: str) -> bool:
    try:
        from receiving_cz.models import ReceivingCzUnit
    except Exception:
        return False
    return ReceivingCzUnit.objects.filter(marking_code=code).exists()


@transaction.atomic
def scan_unit(
    *,
    context: ProcessingCzOrderContext,
    barcode: str,
    marking_code: str,
    box_code: str,
    pallet_code: str,
    user,
    sku_code: str = "",
    size: str = "",
) -> ProcessingCzScanResult:
    if _flow_closed(context.entries):
        return ProcessingCzScanResult(status="closed", error="Раскоробовка уже закрыта.")
    barcode = str(barcode or "").strip()
    box_code = str(box_code or "").strip()
    pallet_code = str(pallet_code or "").strip()
    code = normalize_marking_code(marking_code)
    if not barcode:
        return ProcessingCzScanResult(status="invalid", error="Сначала отсканируйте ШК товара.")
    if not code:
        return ProcessingCzScanResult(status="invalid", error="Код ЧЗ не указан.")
    if code == barcode:
        return ProcessingCzScanResult(status="invalid", error="Сейчас нужен код ЧЗ, а не ШК товара.")
    if not box_code:
        return ProcessingCzScanResult(status="invalid", error="Откройте короб перед сканированием ЧЗ.")
    if not pallet_code:
        return ProcessingCzScanResult(status="invalid", error="Откройте паллет перед сканированием ЧЗ.")
    item = _find_item_by_barcode(context, barcode, sku_code=sku_code, size=size)
    if item is None:
        return ProcessingCzScanResult(status="invalid", error="ШК не найден в товарах после обработки.")

    existing_unit = ProcessingCzUnit.objects.select_for_update().filter(marking_code=code).first()
    if existing_unit:
        if existing_unit.order_id == context.order_id:
            error = "Этот ЧЗ уже отсканирован."
        else:
            error = f"Этот ЧЗ уже отсканирован в обработке {existing_unit.order_id}."
        return ProcessingCzScanResult(status="duplicate", error=error)
    if _receiving_cz_has_code(code):
        return ProcessingCzScanResult(status="duplicate", error="Этот ЧЗ уже отсканирован в приемке.")

    code_variants = marking_code_variants(code)
    existing_mark = (
        MarkingCode.objects.select_for_update()
        .filter(
            Q(identity_key=marking_code_identity(code))
            | Q(code__in=list(code_variants))
        )
        .first()
    )
    now = timezone.localtime()
    if existing_mark:
        if existing_mark.used_at:
            return ProcessingCzScanResult(status="duplicate", error="Код ЧЗ уже использован.")
        if context.agency and existing_mark.agency_id and existing_mark.agency_id != context.agency.id:
            return ProcessingCzScanResult(status="conflict", error="Код ЧЗ принадлежит другому клиенту.")
        if existing_mark.order_type and existing_mark.order_type != "processing":
            return ProcessingCzScanResult(status="conflict", error="Код ЧЗ закреплен в другом процессе.")
        if existing_mark.order_id and existing_mark.order_id != context.order_id:
            return ProcessingCzScanResult(status="conflict", error="Код ЧЗ закреплен за другой заявкой.")
        if existing_mark.sku_code and existing_mark.sku_code != item.sku_code:
            return ProcessingCzScanResult(status="conflict", error="Код ЧЗ относится к другому артикулу.")
        if existing_mark.size and item.size and existing_mark.size != item.size:
            return ProcessingCzScanResult(status="conflict", error="Код ЧЗ относится к другому размеру.")
        existing_mark.order_type = "processing"
        existing_mark.order_id = context.order_id
        existing_mark.agency = context.agency
        existing_mark.sku = item.sku
        existing_mark.sku_code = item.sku_code
        existing_mark.size = item.size
        existing_mark.barcode = barcode
        existing_mark.box_barcode = box_code
        existing_mark.used_at = now
        existing_mark.used_by = user if getattr(user, "is_authenticated", False) else None
        existing_mark.save(
            update_fields=[
                "order_type",
                "order_id",
                "agency",
                "sku",
                "sku_code",
                "size",
                "barcode",
                "box_barcode",
                "used_at",
                "used_by",
            ]
        )
    else:
        MarkingCode.objects.create(
            order_type="processing",
            order_id=context.order_id,
            agency=context.agency,
            sku=item.sku,
            sku_code=item.sku_code,
            size=item.size,
            barcode=barcode,
            box_barcode=box_code,
            code=code,
            source="scan",
            created_by=user if getattr(user, "is_authenticated", False) else None,
            used_at=now,
            used_by=user if getattr(user, "is_authenticated", False) else None,
        )

    unit = ProcessingCzUnit.objects.create(
        order_id=context.order_id,
        agency=context.agency,
        sku=item.sku,
        sku_code=item.sku_code,
        name=item.name,
        size=item.size,
        barcode=barcode,
        marking_code=code,
        box_code=box_code,
        pallet_code=pallet_code,
        accepted_by=user if getattr(user, "is_authenticated", False) else None,
    )
    item_count = ProcessingCzUnit.objects.filter(
        order_id=context.order_id,
        sku_code=item.sku_code,
        size=item.size,
    ).count()
    total_count = ProcessingCzUnit.objects.filter(order_id=context.order_id).count()
    return ProcessingCzScanResult(status="ok", unit=unit, item_count=item_count, total_count=total_count)


def _build_boxes_and_pallets(units: list[ProcessingCzUnit]) -> tuple[list[dict], list[dict]]:
    boxes_by_code: dict[str, dict] = {}
    pallet_boxes: dict[str, set[str]] = {}
    for unit in units:
        box = boxes_by_code.setdefault(
            unit.box_code,
            {
                "code": unit.box_code,
                "items": [],
                "sealed": True,
            },
        )
        box["items"].append(
            {
                "sku_code": unit.sku_code,
                "sku": unit.sku_code,
                "name": unit.name,
                "size": unit.size,
                "barcode": unit.barcode,
                "qty": 1,
                "marking_code": unit.marking_code,
            }
        )
        pallet_boxes.setdefault(unit.pallet_code, set()).add(unit.box_code)
    boxes = [boxes_by_code[key] for key in sorted(boxes_by_code)]
    pallets = [
        {
            "code": pallet_code,
            "boxes": sorted(box_codes),
            "items": [],
            "sealed": True,
            "location": {"zone": "OBR"},
        }
        for pallet_code, box_codes in sorted(pallet_boxes.items())
    ]
    return boxes, pallets


def _parse_submitted_containers(request, field_name: str) -> list[dict]:
    raw_value = request.POST.get(field_name) or "[]"
    try:
        parsed = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


def _flow_session_snapshot(order_id: str) -> tuple[list[dict], list[dict], str]:
    boxes: list[dict] = []
    pallets: list[dict] = []
    received_at = ""
    sessions = ProcessingFlowSession.objects.filter(
        order_id=str(order_id or "").strip(),
        order_type="processing",
        status=ProcessingFlowSession.STATUS_OPEN,
    ).order_by("updated_at", "id")
    for session in sessions:
        state = session.flow_state if isinstance(session.flow_state, dict) else {}
        raw_boxes = state.get("boxes") if isinstance(state.get("boxes"), list) else []
        raw_pallets = state.get("pallets") if isinstance(state.get("pallets"), list) else []
        boxes.extend(entry for entry in raw_boxes if isinstance(entry, dict))
        pallets.extend(entry for entry in raw_pallets if isinstance(entry, dict))
        date_value = str(state.get("received_at") or "").strip()
        if date_value:
            received_at = date_value
    return boxes, pallets, received_at


def _containers_by_code(containers: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for raw in containers or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        if not code:
            continue
        result[code] = dict(raw)
    return result


def _merge_box_metadata(boxes: list[dict], *metadata_sources: list[dict]) -> list[dict]:
    metadata: dict[str, dict] = {}
    for source in metadata_sources:
        metadata.update(_containers_by_code(source))
    merged_boxes: list[dict] = []
    for box in boxes:
        code = str(box.get("code") or "").strip()
        merged = dict(metadata.get(code, {}))
        merged.update(
            {
                "code": code,
                "items": list(box.get("items") or []),
                "sealed": True,
            }
        )
        merged_boxes.append(merged)
    return merged_boxes


def _merge_pallet_metadata(pallets: list[dict], *metadata_sources: list[dict]) -> list[dict]:
    metadata: dict[str, dict] = {}
    for source in metadata_sources:
        metadata.update(_containers_by_code(source))
    merged_pallets: list[dict] = []
    for pallet in pallets:
        code = str(pallet.get("code") or "").strip()
        merged = dict(metadata.get(code, {}))
        location = merged.get("location")
        if isinstance(location, dict):
            location = dict(location)
            location["zone"] = "OBR"
        else:
            location = dict(pallet.get("location") or {"zone": "OBR"})
        merged.update(
            {
                "code": code,
                "boxes": list(pallet.get("boxes") or []),
                "items": [],
                "sealed": True,
                "location": location,
            }
        )
        merged_pallets.append(merged)
    return merged_pallets


def _synthetic_processing_request(request, *, boxes: list[dict], pallets: list[dict], received_at: str = ""):
    post = QueryDict("", mutable=True)
    post["boxes_json"] = json.dumps(boxes, ensure_ascii=False)
    post["pallets_json"] = json.dumps(pallets, ensure_ascii=False)
    post["active_box"] = ""
    post["active_pallet"] = ""
    post["agent_id"] = str(request.POST.get("agent_id") or "").strip()
    post["allow_mismatch"] = str(request.POST.get("allow_mismatch") or "0").strip()
    received_at_value = str(request.POST.get("received_at") or received_at or "").strip()
    if received_at_value:
        post["received_at"] = received_at_value
    return SimpleNamespace(
        POST=post,
        user=request.user,
        session=getattr(request, "session", {}),
        headers=getattr(request, "headers", {}),
        META=getattr(request, "META", {}),
        GET=getattr(request, "GET", {}),
    )


def complete_flow(*, context: ProcessingCzOrderContext, request, role: str) -> tuple[str, str]:
    if _flow_closed(context.entries):
        return "closed", "Раскоробовка уже закрыта."
    units = list(accepted_units(context.order_id).order_by("accepted_at", "id"))
    if not units:
        return "empty", "Нет принятых ЧЗ."
    boxes, pallets = _build_boxes_and_pallets(units)
    submitted_boxes = _parse_submitted_containers(request, "boxes_json")
    submitted_pallets = _parse_submitted_containers(request, "pallets_json")
    session_boxes, session_pallets, session_received_at = _flow_session_snapshot(context.order_id)
    boxes = _merge_box_metadata(boxes, session_boxes, submitted_boxes)
    pallets = _merge_pallet_metadata(pallets, session_pallets, submitted_pallets)
    flow_view = ProcessingFlowView()
    flow_view.kwargs = {"order_id": context.order_id}
    synthetic_request = _synthetic_processing_request(
        request,
        boxes=boxes,
        pallets=pallets,
        received_at=session_received_at,
    )
    result = ProcessingWorkflowService.complete_processing_flow(
        order_id=context.order_id,
        entries=context.entries,
        request=synthetic_request,
        normalize_flow_state=flow_view._normalize_flow_state,
        items_from_placement_act=flow_view._items_from_placement_act,
        can_start=flow_view._can_start(context.entries),
        can_finish_with_mismatch=role == "processing_head",
        can_reassign_boxes=role == "processing_head",
        order_type="processing",
    )
    if result.status == "ok":
        return "ok", ""
    return result.error_code or result.status, result.error_code or result.status


def save_flow_draft(*, context: ProcessingCzOrderContext, request):
    flow_view = ProcessingFlowView()
    flow_view.kwargs = {"order_id": context.order_id}
    return ProcessingWorkflowService.save_processing_flow_draft(
        order_id=context.order_id,
        entries=context.entries,
        request=request,
        normalize_flow_state=flow_view._normalize_flow_state,
        can_reassign_boxes=get_request_role(request) == "processing_head",
    )


def reopen_flow(*, context: ProcessingCzOrderContext, request):
    flow_view = ProcessingFlowView()
    flow_view.kwargs = {"order_id": context.order_id}
    return ProcessingWorkflowService.reopen_processing_flow(
        order_id=context.order_id,
        entries=context.entries,
        request=request,
        normalize_flow_state=flow_view._normalize_flow_state,
    )


def known_box_metrics(context: ProcessingCzOrderContext) -> dict:
    return ReceivingWorkflowService.build_known_box_metrics_defaults(
        agency=context.agency,
        mode="processing",
    )
