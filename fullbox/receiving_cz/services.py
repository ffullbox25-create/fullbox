from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from audit.models import OrderAuditEntry
from fullbox.container_codes import issue_container_code, issue_pallet_code
from marking.codes import (
    MarkingCodeFormatError,
    marking_code_identity,
    marking_code_variants,
    normalize_marking_code as canonicalize_marking_code,
    validate_import_marking_code,
)
from marking.models import MarkingCode
from orders.services import ReceivingWorkflowService
from orders.shipping_returns import (
    SHIPPING_RETURN_GOODS_TYPE,
    marking_code_gtin,
    resolve_shipping_return_order,
    shipping_return_mark_has_stock_history,
    shipping_return_mark_is_live,
    shipping_return_item_key,
    shipping_return_source_item_gtins,
    shipping_return_source_mark_snapshot,
    validate_shipping_return_items,
)
from sku.models import Agency, SKU, SKUBarcode

from .models import ReceivingCzUnit
from .repeat_receiving import lock_shipped_mark_intake, shipped_mark_evidence


WAREHOUSE_MARKING_CODE_MAX_LENGTH = 128
_GS1_GTIN_PREFIX_RE = re.compile(r"^01\d{14}")


@dataclass
class ReceivingCzItem:
    sku_code: str
    name: str = ""
    size: str = ""
    barcode: str = ""
    barcodes: list[str] = field(default_factory=list)
    planned_qty: int = 0
    sku: SKU | None = None

    @property
    def key(self) -> str:
        return f"{self.sku_code.strip().lower()}|{self.size.strip().lower()}"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "sku_code": self.sku_code,
            "name": self.name,
            "size": self.size,
            "barcode": self.barcode,
            "barcodes": list(self.barcodes),
            "planned_qty": self.planned_qty,
            "accepted_qty": 0,
        }


@dataclass
class ReceivingCzOrderContext:
    order_id: str
    entries: list[OrderAuditEntry]
    latest: OrderAuditEntry | None
    agency: Agency | None
    status_payload: dict
    items: list[ReceivingCzItem]


@dataclass
class ReceivingCzScanResult:
    status: str
    error: str = ""
    unit: ReceivingCzUnit | None = None
    item_count: int = 0
    total_count: int = 0
    details: dict = field(default_factory=dict)


@dataclass
class ReceivingCzDeleteResult:
    status: str
    error: str = ""
    unit: dict | None = None
    total_count: int = 0


@dataclass
class ReceivingCzDeleteBoxResult:
    status: str
    error: str = ""
    units: list[dict] = field(default_factory=list)
    total_count: int = 0


def normalize_marking_code(value: str | None) -> str:
    if not value:
        return ""
    text = canonicalize_marking_code(value)
    if len(text) <= WAREHOUSE_MARKING_CODE_MAX_LENGTH:
        return text

    prefix_match = _GS1_GTIN_PREFIX_RE.match(text)
    if not prefix_match:
        return text
    gtin_prefix = prefix_match.group(0)
    repeated_at = text.rfind(gtin_prefix, len(gtin_prefix))
    if repeated_at <= 0:
        return text
    candidate = text[repeated_at:].strip()
    if (
        len(candidate) <= WAREHOUSE_MARKING_CODE_MAX_LENGTH
        and candidate.find(gtin_prefix, len(gtin_prefix)) < 0
        and "\x1d91" in candidate
        and "\x1d92" in candidate
    ):
        return candidate
    return text


def _marking_code_variants(code: str) -> set[str]:
    return marking_code_variants(code)


def _find_receiving_unit_by_mark(queryset, code: str) -> ReceivingCzUnit | None:
    normalized = normalize_marking_code(code)
    if not normalized:
        return None
    variants = _marking_code_variants(normalized)
    identity = marking_code_identity(normalized)
    lookup = models.Q(marking_code__in=variants)
    if identity.startswith("01"):
        lookup |= models.Q(marking_code__startswith=identity)
        lookup |= models.Q(marking_code__startswith=f"]d2{identity}")
        lookup |= models.Q(marking_code__startswith=f"]D2{identity}")
    for existing in queryset.filter(lookup).order_by("id")[:20]:
        if marking_code_identity(existing.marking_code) == identity:
            return existing
    for variant in sorted(variants, key=len, reverse=True):
        candidates = queryset.filter(marking_code__contains=variant).order_by("id")[:5]
        for candidate in candidates:
            if marking_code_identity(candidate.marking_code) == identity:
                return candidate
    return None


def _agency_label(agency: Agency | None) -> str:
    if not agency:
        return ""
    for field_name in ("short_name", "agn_name", "fio_agn"):
        value = str(getattr(agency, field_name, "") or "").strip()
        if value:
            return value
    return str(agency).strip()


def _user_label(user) -> str:
    if not user:
        return ""
    full_name = str(user.get_full_name() or "").strip() if hasattr(user, "get_full_name") else ""
    return full_name or str(getattr(user, "username", "") or "").strip()


def _datetime_label(value) -> str:
    if not value:
        return ""
    try:
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%d.%m.%Y %H:%M")
    except (AttributeError, ValueError):
        return str(value)


def _usage_details(
    *,
    agency=None,
    process: str = "",
    order_id: str = "",
    box_code: str = "",
    pallet_code: str = "",
    used_at=None,
    used_by=None,
) -> dict:
    process_key = str(process or "").strip().lower()
    process_labels = {
        "processing": "Обработка",
        "receiving": "Приемка",
        "placement": "Размещение",
        "shipping": "Отгрузка",
        "other": "Другой процесс",
    }
    return {
        "owner_name": _agency_label(agency),
        "process": process_key,
        "process_label": process_labels.get(process_key, process_key or "Не определен"),
        "order_id": str(order_id or "").strip(),
        "box_code": str(box_code or "").strip(),
        "pallet_code": str(pallet_code or "").strip(),
        "used_at": _datetime_label(used_at),
        "used_by": _user_label(used_by),
    }


def _usage_message(prefix: str, details: dict) -> str:
    parts = [str(prefix or "").strip().rstrip(".") + "."]
    owner_name = str(details.get("owner_name") or "").strip()
    process_label = str(details.get("process_label") or "").strip()
    order_id = str(details.get("order_id") or "").strip()
    box_code = str(details.get("box_code") or "").strip()
    pallet_code = str(details.get("pallet_code") or "").strip()
    used_by = str(details.get("used_by") or "").strip()
    used_at = str(details.get("used_at") or "").strip()
    if owner_name:
        parts.append(f"Клиент: {owner_name}.")
    if process_label and order_id:
        parts.append(f"{process_label}: заявка №{order_id}.")
    elif process_label:
        parts.append(f"Процесс: {process_label}.")
    elif order_id:
        parts.append(f"Заявка №{order_id}.")
    if box_code:
        parts.append(f"Короб: {box_code}.")
    if pallet_code:
        parts.append(f"Палета: {pallet_code}.")
    if used_by:
        parts.append(f"Пользователь: {used_by}.")
    if used_at:
        parts.append(f"Время: {used_at}.")
    return " ".join(parts)


def duplicate_marking_usage(marking_code: str) -> dict:
    code = normalize_marking_code(marking_code)
    if not code:
        return {}
    code_variants = _marking_code_variants(code)
    existing_unit = _find_receiving_unit_by_mark(
        ReceivingCzUnit.objects.select_related("agency", "accepted_by"),
        code,
    )
    if existing_unit:
        details = _usage_details(
            agency=existing_unit.agency,
            process="receiving",
            order_id=existing_unit.order_id,
            box_code=existing_unit.box_code,
            pallet_code=existing_unit.pallet_code,
            used_at=existing_unit.accepted_at,
            used_by=existing_unit.accepted_by,
        )
        return {
            "error": _usage_message("Код ЧЗ уже принят", details),
            **details,
        }

    existing_mark = (
        MarkingCode.objects.select_related("agency", "used_by")
        .filter(
            models.Q(identity_key=marking_code_identity(code))
            | models.Q(code__in=code_variants)
        )
        .order_by("id")
        .first()
    )
    if not existing_mark:
        return {}
    details = _usage_details(
        agency=existing_mark.agency,
        process=existing_mark.order_type,
        order_id=existing_mark.order_id,
        box_code=existing_mark.box_barcode,
        used_at=existing_mark.used_at,
        used_by=existing_mark.used_by,
    )
    return {
        "error": _usage_message("Код ЧЗ уже используется", details),
        **details,
    }


def _parse_qty(value) -> int:
    if value is None:
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _flow_closed(entries: list[OrderAuditEntry]) -> bool:
    for entry in reversed(entries):
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        if payload.get("flow_reopened"):
            return False
        if payload.get("flow_closed"):
            return True
        if payload.get("act") == "receiving" and payload.get("act_state") == "closed":
            return True
    return False


def load_order_context(order_id: str) -> ReceivingCzOrderContext | None:
    order_key = str(order_id or "").strip()
    if not order_key:
        return None
    entries = list(
        OrderAuditEntry.objects.filter(order_id=order_key, order_type="receiving")
        .select_related("agency", "user")
        .order_by("created_at", "id")
    )
    if not entries:
        return None
    latest = entries[-1]
    status_entry = ReceivingWorkflowService._current_status_entry(entries)
    status_payload = dict(status_entry.payload or {}) if status_entry else dict(latest.payload or {})
    agency = latest.agency or (status_entry.agency if status_entry else None)
    return ReceivingCzOrderContext(
        order_id=order_key,
        entries=entries,
        latest=latest,
        agency=agency,
        status_payload=status_payload,
        items=_build_items(agency, status_payload),
    )


def _build_items(agency: Agency | None, payload: dict) -> list[ReceivingCzItem]:
    raw_items = payload.get("items") or []
    rows: dict[tuple[str, str], ReceivingCzItem] = {}
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
            ReceivingCzItem(
                sku_code=sku_code,
                name=str(raw.get("name") or "").strip(),
                size=size,
                barcode=str(raw.get("barcode") or "").strip(),
            ),
        )
        row.planned_qty += _parse_qty(raw.get("qty") if raw.get("qty") is not None else raw.get("actual_qty"))
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


def _catalog_items(context: ReceivingCzOrderContext) -> list[ReceivingCzItem]:
    if str(context.status_payload.get("goods_type") or "").strip().lower() == SHIPPING_RETURN_GOODS_TYPE:
        return []
    if not context.agency:
        return []
    existing = {item.key for item in context.items}
    rows: dict[tuple[str, str], ReceivingCzItem] = {}
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
        key_text = f"{sku_code.lower()}|{size.lower()}"
        if key_text in existing:
            continue
        row = rows.setdefault(
            (sku_code.lower(), size.lower()),
            ReceivingCzItem(
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


def _unit_items(order_id: str, known_keys: set[str]) -> list[ReceivingCzItem]:
    rows: dict[tuple[str, str], ReceivingCzItem] = {}
    for unit in accepted_units(order_id):
        key_text = _item_key(unit.sku_code, unit.size)
        if key_text in known_keys:
            continue
        row = rows.setdefault(
            (unit.sku_code.lower(), unit.size.lower()),
            ReceivingCzItem(
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


def _find_item_by_barcode(context: ReceivingCzOrderContext, barcode: str) -> ReceivingCzItem | None:
    value = str(barcode or "").strip()
    if not value:
        return None
    for item in context.items:
        if value == item.barcode or value in item.barcodes:
            return item
    if str(context.status_payload.get("goods_type") or "").strip().lower() == SHIPPING_RETURN_GOODS_TYPE:
        return None
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
    sku_code = str(barcode_obj.sku.sku_code or "").strip().lower()
    size = str(barcode_obj.size or barcode_obj.sku.size or "").strip().lower()
    matches = [item for item in context.items if item.sku_code.lower() == sku_code]
    exact = next((item for item in matches if item.size.lower() == size), None)
    if exact:
        return exact
    if len(matches) == 1:
        return matches[0]
    return ReceivingCzItem(
        sku_code=str(barcode_obj.sku.sku_code or "").strip(),
        name=barcode_obj.sku.name or "",
        size=str(barcode_obj.size or barcode_obj.sku.size or "").strip(),
        barcode=value,
        barcodes=[value],
        planned_qty=0,
        sku=barcode_obj.sku,
    )


def _barcode_gtin(value: str | None) -> str:
    digits = str(value or "").strip()
    if digits.isdigit() and len(digits) in {8, 12, 13, 14}:
        return digits.zfill(14)
    return ""


def _gtin_barcode_values(gtin: str) -> set[str]:
    normalized = str(gtin or "").strip()
    if len(normalized) != 14 or not normalized.isdigit():
        return set()
    values = {normalized}
    for length in (13, 12, 8):
        candidate = normalized[-length:]
        if candidate.zfill(14) == normalized:
            values.add(candidate)
    return values


def _matching_item_barcode(item: ReceivingCzItem, gtin: str) -> str:
    values = [item.barcode, *item.barcodes]
    return next(
        (
            str(value or "").strip()
            for value in values
            if _barcode_gtin(value) == gtin
        ),
        "",
    )


def _deduplicate_gtin_matches(
    matches: list[tuple[ReceivingCzItem, str]],
) -> list[tuple[ReceivingCzItem, str]]:
    unique: dict[str, tuple[ReceivingCzItem, str]] = {}
    for item, barcode in matches:
        unique.setdefault(item.key, (item, barcode))
    return list(unique.values())


def _resolve_item_by_gtin(
    context: ReceivingCzOrderContext,
    gtin: str,
) -> tuple[ReceivingCzItem | None, str, str, str]:
    planned_matches = _deduplicate_gtin_matches(
        [
            (item, barcode)
            for item in context.items
            if (barcode := _matching_item_barcode(item, gtin))
        ]
    )
    if len(planned_matches) == 1:
        item, barcode = planned_matches[0]
        return item, barcode, "", ""
    if len(planned_matches) > 1:
        return (
            None,
            "",
            (
                f"GTIN {gtin} соответствует нескольким позициям текущей заявки. "
                "Товар не принят: исправьте привязку GTIN в номенклатуре."
            ),
            "ambiguous_gtin",
        )

    if not context.agency:
        return None, "", "Не найден клиент приемки. Товар не принят.", "client_not_found"

    barcode_values = _gtin_barcode_values(gtin)
    barcode_rows = list(
        SKUBarcode.objects.select_related("sku")
        .filter(
            value__in=barcode_values,
            sku__agency=context.agency,
            sku__deleted=False,
        )
        .order_by("sku__sku_code", "-is_primary", "id")
    )
    catalog_matches: list[tuple[ReceivingCzItem, str]] = []
    for barcode_obj in barcode_rows:
        sku = barcode_obj.sku
        sku_code = str(sku.sku_code or "").strip()
        size = str(barcode_obj.size or sku.size or "").strip()
        contextual = [
            item
            for item in context.items
            if item.sku_code.strip().casefold() == sku_code.casefold()
        ]
        item = next(
            (candidate for candidate in contextual if candidate.size.strip().casefold() == size.casefold()),
            contextual[0] if len(contextual) == 1 else None,
        )
        if item is None:
            item = ReceivingCzItem(
                sku_code=sku_code,
                name=sku.name or "",
                size=size,
                barcode=str(barcode_obj.value or "").strip(),
                barcodes=[str(barcode_obj.value or "").strip()],
                planned_qty=0,
                sku=sku,
            )
        catalog_matches.append((item, str(barcode_obj.value or "").strip()))

    catalog_matches = _deduplicate_gtin_matches(catalog_matches)
    if len(catalog_matches) == 1:
        item, barcode = catalog_matches[0]
        return item, barcode, "", ""
    if len(catalog_matches) > 1:
        return (
            None,
            "",
            (
                f"GTIN {gtin} связан с несколькими товарами или размерами клиента. "
                "Товар не принят: исправьте привязку GTIN в номенклатуре."
            ),
            "ambiguous_gtin",
        )
    return (
        None,
        "",
        f"GTIN {gtin} не найден в номенклатуре клиента. Товар не принят.",
        "unknown_gtin",
    )


def serialize_unit(unit: ReceivingCzUnit) -> dict:
    return {
        "id": unit.id,
        "sku_code": unit.sku_code,
        "name": unit.name,
        "size": unit.size,
        "barcode": unit.barcode,
        "marking_code": normalize_marking_code(unit.marking_code),
        "box_code": unit.box_code,
        "pallet_code": unit.pallet_code,
        "source_shipping_order_number": unit.source_shipping_order_number,
        "source_mark_verified": unit.source_mark_verified,
        "source_discrepancy_reason": unit.source_discrepancy_reason,
        "accepted_at": unit.accepted_at.isoformat() if unit.accepted_at else "",
        "accepted_by": _user_label(unit.accepted_by),
    }


def accepted_units(order_id: str):
    return ReceivingCzUnit.objects.filter(order_id=str(order_id or "").strip()).select_related(
        "sku",
        "agency",
        "accepted_by",
    )


def reconcile_flow_state_with_units(
    flow_state: dict | None,
    units: list[ReceivingCzUnit],
    *,
    active_box: str = "",
    active_pallet: str = "",
) -> dict:
    source_state = dict(flow_state or {}) if isinstance(flow_state, dict) else {}
    boxes = []
    boxes_by_code: dict[str, dict] = {}
    item_rows_by_box: dict[str, dict[tuple[str, str, str, str], dict]] = {}
    goods_type_by_box: dict[str, str] = {}
    for raw_box in source_state.get("boxes") or []:
        if not isinstance(raw_box, dict):
            continue
        box_code = str(raw_box.get("code") or "").strip()
        if not box_code or box_code in boxes_by_code:
            continue
        box = dict(raw_box)
        box["code"] = box_code
        boxes.append(box)
        boxes_by_code[box_code] = box
        item_rows = {}
        for raw_item in raw_box.get("items") or []:
            if not isinstance(raw_item, dict):
                continue
            item_key = (
                str(raw_item.get("sku_code") or raw_item.get("sku") or "").strip(),
                str(raw_item.get("name") or "").strip(),
                str(raw_item.get("size") or "").strip(),
                str(raw_item.get("barcode") or "").strip(),
            )
            item_rows[item_key] = raw_item
            goods_type = str(raw_item.get("goods_type") or "").strip()
            if goods_type:
                goods_type_by_box.setdefault(box_code, goods_type)
        item_rows_by_box[box_code] = item_rows

    rows_by_box: dict[str, dict[tuple[str, str, str, str], dict]] = {}
    pallet_by_box: dict[str, str] = {}
    for unit in units:
        box_code = str(unit.box_code or "").strip()
        if not box_code:
            continue
        sku_code = str(unit.sku_code or "").strip()
        name = str(unit.name or "").strip()
        size = str(unit.size or "").strip()
        barcode = str(unit.barcode or "").strip()
        row_key = (sku_code, name, size, barcode)
        box_rows = rows_by_box.setdefault(box_code, {})
        row = box_rows.get(row_key)
        if row is None:
            sku = unit.sku
            row = {
                "sku_code": sku_code,
                "sku": sku_code,
                "barcode": barcode,
                "name": name,
                "brand": str(getattr(sku, "brand", "") or "").strip(),
                "color": str(getattr(sku, "color", "") or "").strip(),
                "size": size,
                "qty": 0,
            }
            existing_item = item_rows_by_box.get(box_code, {}).get(row_key) or {}
            goods_type = str(
                existing_item.get("goods_type") or goods_type_by_box.get(box_code) or ""
            ).strip()
            if goods_type:
                row["goods_type"] = goods_type
            box_rows[row_key] = row
        row["qty"] += 1
        pallet_code = str(unit.pallet_code or "").strip()
        if pallet_code:
            pallet_by_box.setdefault(box_code, pallet_code)

    requested_active_box = str(active_box or "").strip()
    for box_code, box_rows in rows_by_box.items():
        box = boxes_by_code.get(box_code)
        if box is None:
            box = {
                "code": box_code,
                "sealed": bool(requested_active_box and box_code != requested_active_box),
            }
            boxes.append(box)
            boxes_by_code[box_code] = box
        box["items"] = list(box_rows.values())
    for box in boxes:
        if box["code"] not in rows_by_box:
            box["items"] = []

    pallets = []
    pallets_by_code: dict[str, dict] = {}
    for raw_pallet in source_state.get("pallets") or []:
        if not isinstance(raw_pallet, dict):
            continue
        pallet_code = str(raw_pallet.get("code") or "").strip()
        if not pallet_code or pallet_code in pallets_by_code:
            continue
        pallet = dict(raw_pallet)
        pallet["code"] = pallet_code
        pallet["boxes"] = [
            str(box_code or "").strip()
            for box_code in pallet.get("boxes") or []
            if str(box_code or "").strip()
        ]
        pallets.append(pallet)
        pallets_by_code[pallet_code] = pallet

    requested_active_pallet = str(active_pallet or "").strip()
    for box_code, pallet_code in pallet_by_box.items():
        for pallet in pallets:
            if pallet["code"] != pallet_code:
                pallet["boxes"] = [code for code in pallet["boxes"] if code != box_code]
        pallet = pallets_by_code.get(pallet_code)
        if pallet is None:
            pallet = {
                "code": pallet_code,
                "boxes": [],
                "items": [],
                "sealed": bool(requested_active_pallet and pallet_code != requested_active_pallet),
                "location": {"zone": "PR"},
            }
            pallets.append(pallet)
            pallets_by_code[pallet_code] = pallet
        if box_code not in pallet["boxes"]:
            pallet["boxes"].append(box_code)

    resolved_active_box = requested_active_box or str(source_state.get("activeBox") or "").strip()
    if resolved_active_box not in boxes_by_code:
        resolved_active_box = next(
            (box["code"] for box in boxes if not box.get("sealed")),
            "",
        )
    resolved_active_pallet = requested_active_pallet or str(source_state.get("activePallet") or "").strip()
    if resolved_active_pallet not in pallets_by_code:
        resolved_active_pallet = next(
            (pallet["code"] for pallet in pallets if not pallet.get("sealed")),
            "",
        )

    source_state.update(
        {
            "boxes": boxes,
            "pallets": pallets,
            "activeBox": resolved_active_box,
            "activePallet": resolved_active_pallet,
        }
    )
    return source_state


def _release_marking_code_for_unit(*, context: ReceivingCzOrderContext, unit: ReceivingCzUnit) -> None:
    if unit.source_shipping_order_number:
        return
    mark = MarkingCode.objects.select_for_update().filter(
        code=unit.marking_code,
        order_type="receiving",
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
def delete_unit(
    *,
    context: ReceivingCzOrderContext,
    unit_id: int,
) -> ReceivingCzDeleteResult:
    if _flow_closed(context.entries):
        return ReceivingCzDeleteResult(status="closed", error="Приемка уже закрыта.")
    unit = (
        ReceivingCzUnit.objects.select_for_update()
        .filter(order_id=context.order_id, id=unit_id)
        .first()
    )
    if unit is None:
        return ReceivingCzDeleteResult(status="not_found", error="ЧЗ не найден в этой приемке.")

    unit_payload = serialize_unit(unit)
    _release_marking_code_for_unit(context=context, unit=unit)
    unit.delete()
    total_count = ReceivingCzUnit.objects.filter(order_id=context.order_id).count()
    return ReceivingCzDeleteResult(status="ok", unit=unit_payload, total_count=total_count)


@transaction.atomic
def delete_units_for_box(
    *,
    context: ReceivingCzOrderContext,
    box_code: str,
) -> ReceivingCzDeleteBoxResult:
    if _flow_closed(context.entries):
        return ReceivingCzDeleteBoxResult(status="closed", error="Приемка уже закрыта.")
    normalized_box_code = str(box_code or "").strip()
    if not normalized_box_code:
        return ReceivingCzDeleteBoxResult(status="invalid", error="box_code_required")
    units = list(
        ReceivingCzUnit.objects.select_for_update()
        .filter(order_id=context.order_id, box_code=normalized_box_code)
        .order_by("accepted_at", "id")
    )
    unit_payloads = [serialize_unit(unit) for unit in units]
    for unit in units:
        _release_marking_code_for_unit(context=context, unit=unit)
        unit.delete()
    total_count = ReceivingCzUnit.objects.filter(order_id=context.order_id).count()
    return ReceivingCzDeleteBoxResult(status="ok", units=unit_payloads, total_count=total_count)


def context_payload(context: ReceivingCzOrderContext) -> dict:
    units = list(accepted_units(context.order_id).order_by("accepted_at", "id"))
    counts: dict[str, int] = {}
    for unit in units:
        key = f"{unit.sku_code.strip().lower()}|{unit.size.strip().lower()}"
        counts[key] = counts.get(key, 0) + 1
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
        "units": [serialize_unit(unit) for unit in units],
        "total_count": len(units),
    }


def issue_container_for_order(context: ReceivingCzOrderContext, *, kind: str, received_at: str = "") -> dict:
    if not context.agency:
        return {"ok": False, "error": "client_not_found"}
    goods_type = str(context.status_payload.get("goods_type") or "").strip().lower()
    if str(kind or "").strip().lower() == "pallet":
        return {
            "ok": True,
            **issue_pallet_code(
                agency=context.agency,
                goods_type=goods_type,
                order_type="receiving",
                order_id=context.order_id,
            ),
        }
    return {
        "ok": True,
        **issue_container_code(
            agency=context.agency,
            goods_type=goods_type,
            received_at=received_at,
        ),
    }


@transaction.atomic
def scan_unit(
    *,
    context: ReceivingCzOrderContext,
    barcode: str,
    marking_code: str,
    box_code: str,
    pallet_code: str,
    user,
    confirm_gtin_mismatch: bool = False,
) -> ReceivingCzScanResult:
    if _flow_closed(context.entries):
        return ReceivingCzScanResult(status="closed", error="Приемка уже закрыта.")
    barcode = str(barcode or "").strip()
    confirm_gtin_mismatch = confirm_gtin_mismatch is True
    box_code = str(box_code or "").strip()
    pallet_code = str(pallet_code or "").strip()
    code = normalize_marking_code(marking_code)
    if not code:
        return ReceivingCzScanResult(status="invalid", error="Код ЧЗ не указан.")
    if len(code) > WAREHOUSE_MARKING_CODE_MAX_LENGTH:
        return ReceivingCzScanResult(
            status="invalid",
            error=(
                "Сканер передал поврежденный или склеенный код ЧЗ длиннее 128 символов. "
                "Повторите сканирование одного Data Matrix."
            ),
        )
    if not box_code:
        return ReceivingCzScanResult(status="invalid", error="Откройте короб перед сканированием ЧЗ.")
    if not pallet_code:
        return ReceivingCzScanResult(status="invalid", error="Откройте паллет перед сканированием ЧЗ.")
    if confirm_gtin_mismatch and not code.startswith("01"):
        return ReceivingCzScanResult(
            status="invalid",
            error=(
                "Подтверждение несовпадения GTIN возможно только для полного DataMatrix ЧЗ "
                "с GTIN и серийным номером. Товар не принят."
            ),
            details={"reason_code": "full_marking_code_required"},
        )

    item = None
    gtin_mismatch_accepted = False
    scanned_product_barcode = barcode
    marking_gtin = marking_code_gtin(code)
    if code.startswith("01"):
        try:
            code = validate_import_marking_code(code)
        except MarkingCodeFormatError as exc:
            return ReceivingCzScanResult(
                status="invalid",
                error=f"Некорректный DataMatrix ЧЗ: {exc}. Товар не принят.",
                details={"reason_code": "invalid_marking_code"},
            )
        marking_gtin = marking_code_gtin(code)
        gtin_item, resolved_barcode, resolution_error, reason_code = _resolve_item_by_gtin(
            context,
            marking_gtin,
        )
        barcode_item = _find_item_by_barcode(context, barcode) if barcode else None
        if barcode and barcode_item is None:
            return ReceivingCzScanResult(
                status="invalid",
                error="ШК товара не найден в номенклатуре клиента. Товар не принят.",
                details={
                    "reason_code": "unknown_product_barcode",
                    "product_barcode": barcode,
                    "marking_gtin": marking_gtin,
                },
            )

        gtin_matches_barcode_item = bool(
            gtin_item is not None
            and barcode_item is not None
            and gtin_item.key == barcode_item.key
        )
        requires_mismatch_confirmation = bool(
            barcode_item is not None
            and (gtin_item is None or not gtin_matches_barcode_item)
        )
        unknown_gtin_with_scanned_barcode = bool(
            barcode_item is not None
            and gtin_item is None
            and reason_code == "unknown_gtin"
        )
        if (
            requires_mismatch_confirmation
            and str(context.status_payload.get("goods_type") or "").strip().lower()
            == SHIPPING_RETURN_GOODS_TYPE
        ):
            return ReceivingCzScanResult(
                status="conflict",
                error=(
                    "Для возврата из отгрузки нельзя принять ЧЗ с несовпадающим GTIN. "
                    "Проверьте товар и выбранную отгрузку."
                ),
                details={
                    "reason_code": "shipping_return_marking_gtin_mismatch",
                    "marking_gtin": marking_gtin,
                    "product_barcode": barcode,
                },
            )
        if unknown_gtin_with_scanned_barcode:
            item = barcode_item
        elif requires_mismatch_confirmation and not confirm_gtin_mismatch:
            return ReceivingCzScanResult(
                status="conflict",
                error=(
                    f"GTIN {marking_gtin} из DataMatrix не соответствует отсканированному ШК "
                    f"{barcode}. Проверьте товар и отдельно подтвердите приемку."
                ),
                details={
                    "reason_code": "marking_gtin_mismatch_confirmation_required",
                    "marking_gtin": marking_gtin,
                    "product_barcode": barcode,
                    "product_sku_code": barcode_item.sku_code,
                    "product_name": barcode_item.name,
                    "product_size": barcode_item.size,
                    "gtin_resolution_reason": reason_code or "different_product",
                    "gtin_sku_code": gtin_item.sku_code if gtin_item else "",
                    "gtin_product_name": gtin_item.name if gtin_item else "",
                    "gtin_product_size": gtin_item.size if gtin_item else "",
                },
            )
        elif requires_mismatch_confirmation:
            item = barcode_item
            gtin_mismatch_accepted = True
        elif gtin_item is None:
            status = "conflict" if reason_code == "ambiguous_gtin" else "invalid"
            return ReceivingCzScanResult(
                status=status,
                error=resolution_error,
                details={"reason_code": reason_code, "marking_gtin": marking_gtin},
            )
        else:
            item = gtin_item
        barcode = scanned_product_barcode or resolved_barcode
    else:
        if not barcode:
            return ReceivingCzScanResult(
                status="invalid",
                error=(
                    "Отсканирован не полный DataMatrix ЧЗ. Нужен код с GTIN и серийным номером; "
                    "товар не принят."
                ),
                details={"reason_code": "full_marking_code_required"},
            )
        if code == barcode:
            return ReceivingCzScanResult(status="invalid", error="Сейчас нужен код ЧЗ, а не ШК товара.")
        item = _find_item_by_barcode(context, barcode)
        if item is None:
            if str(context.status_payload.get("goods_type") or "").strip().lower() == SHIPPING_RETURN_GOODS_TYPE:
                return ReceivingCzScanResult(
                    status="invalid",
                    error="Этот товар не входил в выбранную отгрузку.",
                )
            return ReceivingCzScanResult(status="invalid", error="ШК не найден в номенклатуре клиента.")

    code_variants = _marking_code_variants(code)
    is_shipping_return = (
        str(context.status_payload.get("goods_type") or "").strip().lower()
        == SHIPPING_RETURN_GOODS_TYPE
    )
    source_order = None
    source_number = ""
    source_mark_verified = True
    source_discrepancy_reason = ""
    repeat_evidence = None
    is_repeat_intake = not is_shipping_return and lock_shipped_mark_intake(context)
    if is_repeat_intake:
        repeat_evidence = shipped_mark_evidence(context, item, code)
    if is_shipping_return:
        source_order = resolve_shipping_return_order(
            agency=context.agency,
            number=context.status_payload.get("shipping_return_order_number"),
            for_update=True,
        )
        if source_order is None:
            return ReceivingCzScanResult(
                status="invalid",
                error="Не найдена выбранная отгрузка для возврата.",
            )
        source_number = source_order.number
        source_snapshot = shipping_return_source_mark_snapshot(source_order, code)
        if shipping_return_mark_is_live(source_order, code):
            return ReceivingCzScanResult(
                status="duplicate",
                error="Этот код ЧЗ уже возвращен и находится на складе.",
            )
        if source_snapshot is None:
            scanned_gtin = marking_code_gtin(code)
            source_gtins = shipping_return_source_item_gtins(
                source_order,
                sku_code=item.sku_code,
                size=item.size,
            )
            same_source_product = bool(scanned_gtin and scanned_gtin in source_gtins)
            if not same_source_product:
                return ReceivingCzScanResult(
                    status="conflict",
                    error="Код ЧЗ относится к другому товару в выбранной отгрузке.",
                )

            known_unit = _find_receiving_unit_by_mark(
                ReceivingCzUnit.objects.select_for_update(),
                code,
            ) is not None
            known_mark = MarkingCode.objects.select_for_update().filter(
                models.Q(identity_key=marking_code_identity(code))
                | models.Q(code__in=code_variants),
            ).exists()
            known_stock = shipping_return_mark_has_stock_history(source_order, code)
            if known_unit or known_mark or known_stock:
                return ReceivingCzScanResult(
                    status="conflict",
                    error="Этот ЧЗ уже зарегистрирован в системе, но не относится к выбранной отгрузке.",
                    details={
                        "reason_code": "shipping_return_known_mark_not_in_source",
                        "process": "shipping",
                        "process_label": "Отгрузка",
                        "order_id": source_order.number,
                        "shipping_return_order_number": source_order.number,
                        "sku_code": item.sku_code,
                        "size": item.size,
                        "marking_gtin": scanned_gtin,
                    },
                )
            source_mark_verified = False
            source_discrepancy_reason = "new_mark_registered_on_shipping_return"
        elif (
            str(source_snapshot.sku_code or "").strip().casefold()
            != item.sku_code.strip().casefold()
            or (
                str(source_snapshot.size or "").strip()
                and item.size.strip()
                and str(source_snapshot.size or "").strip().casefold()
                != item.size.strip().casefold()
            )
        ):
            return ReceivingCzScanResult(
                status="conflict",
                error="Код ЧЗ относится к другому товару в выбранной отгрузке.",
            )
        existing_unit = _find_receiving_unit_by_mark(
            ReceivingCzUnit.objects.select_for_update().filter(
                models.Q(order_id=context.order_id)
                | models.Q(source_shipping_order_number=source_number)
            ),
            code,
        )
    else:
        duplicate_units = ReceivingCzUnit.objects.select_for_update()
        if is_repeat_intake:
            duplicate_units = duplicate_units.filter(order_id=context.order_id)
        existing_unit = _find_receiving_unit_by_mark(
            duplicate_units, code,
        )
        if is_repeat_intake and existing_unit is None and repeat_evidence is None:
            existing_unit = _find_receiving_unit_by_mark(
                ReceivingCzUnit.objects.select_for_update(), code,
            )
    if existing_unit:
        details = _usage_details(
            agency=existing_unit.agency,
            process="receiving",
            order_id=existing_unit.order_id,
            box_code=existing_unit.box_code,
            pallet_code=existing_unit.pallet_code,
            used_at=existing_unit.accepted_at,
            used_by=existing_unit.accepted_by,
        )
        prefix = (
            "Этот ЧЗ уже отсканирован в этой приемке"
            if existing_unit.order_id == context.order_id
            else (
                "Этот ЧЗ уже возвращен по выбранной отгрузке"
                if is_shipping_return
                else "Этот ЧЗ уже принят в другой приемке"
            )
        )
        return ReceivingCzScanResult(
            status="duplicate",
            error=_usage_message(prefix, details),
            unit=existing_unit,
            details=details,
        )

    existing_mark = (
        MarkingCode.objects.select_for_update()
        .filter(
            models.Q(identity_key=marking_code_identity(code))
            | models.Q(code__in=code_variants)
        )
        .order_by("id")
        .first()
    )
    now = timezone.localtime()
    processing_reuse = False
    if existing_mark:
        details = _usage_details(
            agency=existing_mark.agency,
            process=existing_mark.order_type,
            order_id=existing_mark.order_id,
            box_code=existing_mark.box_barcode,
            used_at=existing_mark.used_at,
            used_by=existing_mark.used_by,
        )
        if context.agency and existing_mark.agency_id and existing_mark.agency_id != context.agency.id:
            return ReceivingCzScanResult(
                status="conflict",
                error=_usage_message("Код ЧЗ принадлежит другому клиенту", details),
                details=details,
            )
        if (
            existing_mark.sku_code
            and existing_mark.sku_code.strip().casefold() != item.sku_code.strip().casefold()
        ):
            return ReceivingCzScanResult(
                status="conflict",
                error=_usage_message("Код ЧЗ относится к другому артикулу", details),
                details=details,
            )
        if (
            existing_mark.size
            and item.size
            and existing_mark.size.strip().casefold() != item.size.strip().casefold()
        ):
            return ReceivingCzScanResult(
                status="conflict",
                error=_usage_message("Код ЧЗ относится к другому размеру", details),
                details=details,
            )

        if not is_shipping_return and repeat_evidence is None:
            processing_reuse = bool(
                existing_mark.order_type == "processing"
                and existing_mark.used_at
                and (not context.agency or not existing_mark.agency_id or existing_mark.agency_id == context.agency.id)
            )
            if existing_mark.order_type == "processing" and not processing_reuse:
                return ReceivingCzScanResult(
                    status="conflict",
                    error=_usage_message(
                        "Код ЧЗ закреплен за обработкой, но обработка этой единицы еще не подтверждена",
                        details,
                    ),
                    details=details,
                )
            if existing_mark.order_type not in {"", "receiving", "processing"}:
                return ReceivingCzScanResult(
                    status="conflict",
                    error=_usage_message("Код ЧЗ закреплен в другом процессе", details),
                    details=details,
                )
            if not processing_reuse and existing_mark.used_at:
                return ReceivingCzScanResult(
                    status="duplicate",
                    error=_usage_message("Код ЧЗ уже использован", details),
                    details=details,
                )
            if (
                not processing_reuse
                and existing_mark.order_id
                and existing_mark.order_id != context.order_id
            ):
                return ReceivingCzScanResult(
                    status="conflict",
                    error=_usage_message("Код ЧЗ закреплен за другой заявкой", details),
                    details=details,
                )
            if not processing_reuse:
                existing_mark.order_type = "receiving"
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
            order_type="receiving",
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

    unit = ReceivingCzUnit.objects.create(
        order_id=context.order_id,
        agency=context.agency,
        sku=item.sku,
        sku_code=item.sku_code,
        name=item.name,
        size=item.size,
        barcode=barcode,
        marking_code=code,
        source_shipping_order_number=source_number,
        source_mark_verified=source_mark_verified,
        source_discrepancy_reason=source_discrepancy_reason,
        box_code=box_code,
        pallet_code=pallet_code,
        accepted_by=user if getattr(user, "is_authenticated", False) else None,
    )

    if repeat_evidence is not None:
        OrderAuditEntry.objects.create(
            order_id=context.order_id, order_type="receiving", action="update",
            agency=context.agency,
            user=user if getattr(user, "is_authenticated", False) else None,
            description=f"Повторно принят ранее отгруженный ЧЗ из {repeat_evidence['shipping_order_number']}",
            payload={
                "event_code": "receiving_shipped_mark_readmitted",
                **repeat_evidence,
                "marking_identity": marking_code_identity(code),
                "receiving_unit_id": unit.pk,
                "sku_code": item.sku_code, "size": item.size,
                "box_code": box_code, "pallet_code": pallet_code,
            },
        )

    if gtin_mismatch_accepted:
        OrderAuditEntry.objects.create(
            order_id=context.order_id,
            order_type="receiving",
            action="update",
            agency=context.agency,
            user=user if getattr(user, "is_authenticated", False) else None,
            description=(
                f"Принят ЧЗ с несовпадающим GTIN для {item.sku_code} "
                f"по подтвержденному ШК {barcode}"
            ),
            payload={
                "event_code": "receiving_marking_gtin_mismatch_accepted",
                "sku_code": item.sku_code,
                "size": item.size,
                "barcode": barcode,
                "marking_gtin": marking_gtin,
                "marking_identity": marking_code_identity(code),
                "box_code": box_code,
                "pallet_code": pallet_code,
                "confirmed_by_user_id": getattr(user, "pk", None),
            },
        )

    if is_shipping_return and not source_mark_verified:
        OrderAuditEntry.objects.create(
            order_id=context.order_id,
            order_type="receiving",
            action="update",
            agency=context.agency,
            user=user if getattr(user, "is_authenticated", False) else None,
            description=f"Новый ЧЗ зарегистрирован при возврате из {source_number}",
            payload={
                "event_code": "shipping_return_new_mark_registered",
                "shipping_return_order_number": source_number,
                "sku_code": item.sku_code,
                "size": item.size,
                "barcode": barcode,
                "marking_code": code,
                "box_code": box_code,
                "pallet_code": pallet_code,
                "source_mark_verified": False,
                "source_discrepancy_reason": source_discrepancy_reason,
            },
        )

    item_count = ReceivingCzUnit.objects.filter(
        order_id=context.order_id,
        sku_code=item.sku_code,
        size=item.size,
    ).count()
    total_count = ReceivingCzUnit.objects.filter(order_id=context.order_id).count()
    return ReceivingCzScanResult(
        status="ok",
        unit=unit,
        item_count=item_count,
        total_count=total_count,
        details={
            "gtin_mismatch_accepted": gtin_mismatch_accepted,
            "marking_gtin": marking_gtin if gtin_mismatch_accepted else "",
        },
    )


def _item_key(sku_code: str, size: str) -> str:
    return f"{str(sku_code or '').strip().lower()}|{str(size or '').strip().lower()}"


def _aggregate_items(units: list[ReceivingCzUnit]) -> list[dict]:
    rows: dict[tuple[str, str, str, str], dict] = {}
    for unit in units:
        key = (unit.sku_code, unit.name, unit.size, unit.barcode)
        row = rows.setdefault(
            key,
            {
                "sku_code": unit.sku_code,
                "name": unit.name,
                "size": unit.size,
                "barcode": unit.barcode,
                "qty": 0,
                "actual_qty": 0,
            },
        )
        row["qty"] += 1
        row["actual_qty"] += 1
    return list(rows.values())


def _build_boxes_and_pallets(units: list[ReceivingCzUnit]) -> tuple[list[dict], list[dict]]:
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
                "name": unit.name,
                "size": unit.size,
                "barcode": unit.barcode,
                "qty": 1,
                "marking_code": normalize_marking_code(unit.marking_code),
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
            "location": {"zone": "PR"},
        }
        for pallet_code, box_codes in sorted(pallet_boxes.items())
    ]
    return boxes, pallets


def complete_flow(
    *,
    context: ReceivingCzOrderContext,
    user,
    received_at: str = "",
    receiving_location_code: str = "",
) -> tuple[str, str]:
    if _flow_closed(context.entries):
        return "closed", "Приемка уже закрыта."
    units = list(accepted_units(context.order_id).order_by("accepted_at", "id"))
    if not units:
        return "empty", "Нет принятых ЧЗ."

    planned_by_key = {item.key: item for item in context.items}
    is_shipping_return = (
        str(context.status_payload.get("goods_type") or "").strip().lower()
        == SHIPPING_RETURN_GOODS_TYPE
    )
    actual_by_key: dict[str, int] = {}
    for unit in units:
        key = _item_key(unit.sku_code, unit.size)
        actual_by_key[key] = actual_by_key.get(key, 0) + 1

    act_items = []
    has_mismatch = False
    seen_keys = set()
    for item in context.items:
        actual_qty = int(actual_by_key.get(item.key, 0))
        planned_qty = int(item.planned_qty or 0)
        if not is_shipping_return and actual_qty != planned_qty:
            has_mismatch = True
        seen_keys.add(item.key)
        act_items.append(
            {
                "sku_code": item.sku_code,
                "name": item.name,
                "size": item.size,
                "barcode": item.barcode,
                "planned_qty": planned_qty,
                "actual_qty": actual_qty,
                "comment": "",
            }
        )
    for key, actual_qty in actual_by_key.items():
        if key in seen_keys:
            continue
        if not is_shipping_return:
            has_mismatch = True
        unit = next((candidate for candidate in units if _item_key(candidate.sku_code, candidate.size) == key), None)
        if unit:
            act_items.append(
                {
                    "sku_code": unit.sku_code,
                    "name": unit.name,
                    "size": unit.size,
                    "barcode": unit.barcode,
                    "planned_qty": 0,
                    "actual_qty": actual_qty,
                    "comment": "",
                    "extra": True,
                }
            )

    if is_shipping_return:
        source_order = resolve_shipping_return_order(
            agency=context.agency,
            number=context.status_payload.get("shipping_return_order_number"),
        )
        if source_order is None:
            return "invalid", "Не найдена выбранная отгрузка для возврата."
        if any(unit.source_shipping_order_number != source_order.number for unit in units):
            return "invalid", "В приемке есть ЧЗ не из выбранной отгрузки."
        new_mark_allowance: dict[tuple[str, str], int] = {}
        for unit in units:
            if unit.source_mark_verified:
                continue
            item_key = shipping_return_item_key(unit.sku_code, unit.size)
            new_mark_allowance[item_key] = new_mark_allowance.get(item_key, 0) + 1
        valid_return, return_reason, _return_details = validate_shipping_return_items(
            source_order,
            act_items,
            exclude_receiving_order_id=context.order_id,
            allowed_overage_by_item=new_mark_allowance,
        )
        if not valid_return:
            if return_reason == "shipping_return_qty_exceeded":
                return "invalid", "Количество возврата превышает отгруженное количество."
            return "invalid", "В приемке есть товар не из выбранной отгрузки."

    act_units = [
        {
            "sku_code": unit.sku_code,
            "name": unit.name,
            "size": unit.size,
            "barcode": unit.barcode,
            "marking_code": normalize_marking_code(unit.marking_code),
            "box_code": unit.box_code,
            "pallet_code": unit.pallet_code,
            "source_shipping_order_number": unit.source_shipping_order_number,
            "source_mark_verified": unit.source_mark_verified,
            "source_discrepancy_reason": unit.source_discrepancy_reason,
            "qty": 1,
        }
        for unit in units
    ]
    boxes, pallets = _build_boxes_and_pallets(units)
    flow_state = {
        "boxes": boxes,
        "pallets": pallets,
        "activeBox": "",
        "activePallet": "",
    }
    status_payload = dict(context.status_payload or {})
    status_payload["receiving_mode"] = "cz"
    ReceivingWorkflowService.complete_receiving_flow(
        order_id=context.order_id,
        agency=context.agency,
        status_payload=status_payload,
        has_mismatch=has_mismatch,
        receiving_mode="cz",
        act_items=act_items,
        placement_items=_aggregate_items(units),
        boxes=boxes,
        pallets=pallets,
        flow_state=flow_state,
        act_units=act_units,
        received_at=str(received_at or "").strip(),
        receiving_location_code=str(receiving_location_code or "").strip(),
        user=user if getattr(user, "is_authenticated", False) else None,
        submitted_at=timezone.localtime(),
    )
    return "ok", ""
