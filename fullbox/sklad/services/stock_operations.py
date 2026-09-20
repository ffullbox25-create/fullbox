from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Q

from sku.models import Agency, SKU, SKUBarcode
from sklad.models import WarehouseContainer, WarehouseEvent, WarehouseReserve, WarehouseStockSnapshot

from .warehouse_transitions import WarehouseStateCode
from .warehouse_write_path import WarehouseWritePathService


def _as_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _normalize_zone(zone: str | None) -> str:
    return str(zone or "").strip().upper() or "PR"


def _location_dict(*, zone: str, row: int = 0, section: int = 0, tier: int = 0, cell: int = 0) -> dict:
    normalized_zone = _normalize_zone(zone)
    return {
        "zone": normalized_zone,
        "row": row if row > 0 else "",
        "section": section if section > 0 else "",
        "tier": tier if tier > 0 else "",
        "cell": cell if cell > 0 else "",
    }


def _location_from_row(row) -> dict:
    return _location_dict(
        zone=row.zone,
        row=int(row.row or 0),
        section=int(row.section or 0),
        tier=int(row.tier or 0),
        cell=int(row.cell or 0),
    )


def _location_from_payload(location: dict | None) -> dict:
    location = location or {}
    return _location_dict(
        zone=location.get("zone") or "PR",
        row=_as_int(location.get("row")),
        section=_as_int(location.get("section")),
        tier=_as_int(location.get("tier")),
        cell=_as_int(location.get("cell")),
    )


def _container_location(container: dict | None, default_zone: str) -> dict:
    container = container or {}
    return _location_dict(
        zone=container.get("zone") or container.get("location", {}).get("zone") or default_zone,
        row=_as_int(container.get("row") or container.get("location", {}).get("row")),
        section=_as_int(container.get("section") or container.get("location", {}).get("section")),
        tier=_as_int(container.get("tier") or container.get("location", {}).get("tier")),
        cell=_as_int(container.get("cell") or container.get("location", {}).get("cell")),
    )


def _location_label(location: dict) -> str:
    zone = _normalize_zone(location.get("zone"))
    row = _as_int(location.get("row"))
    section = _as_int(location.get("section"))
    tier = _as_int(location.get("tier"))
    cell = _as_int(location.get("cell"))
    if zone == "OBR":
        return "OBR · Зона обработки"
    if zone == "PR":
        return "PR · Зона приемки"
    if zone == "OTG":
        return "OTG · Зона отгрузки"
    if zone == "MR":
        return f"MR · Между рядами · Ряд {row}" if row else "MR · Между рядами"
    if zone == "OS":
        if row and section and tier and cell:
            return f"OS · Ряд {row} · Секция {section} · Ярус {tier} · Ячейка {cell}"
        if row:
            return f"OS · Ряд {row}"
        return "OS · Основной склад"
    return zone


def _parse_qty(raw) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _clean_barcode(value) -> str:
    text = str(value or "").strip()
    if text in {"-", "вЂ“", "вЂ”"}:
        return ""
    return text


def _goods_type_label(order_type: str, payload: dict | None) -> str:
    payload = payload or {}
    raw_type = str(payload.get("goods_type") or "").strip().lower()
    code_map = {
        "op": "op",
        "gv": "gv",
        "br": "br",
        "vz": "vz",
        "votg": "votg",
        "rh": "rh",
        "no": "no",
        "оптовый": "op",
        "готовый": "gv",
        "брак": "br",
        "возврат": "vz",
        "возврат с отгрузки": "votg",
        "расходный": "rh",
        "не обработанный": "no",
        "необработанный": "no",
    }
    if raw_type in code_map:
        return code_map[raw_type]
    raw_label = str(payload.get("goods_type_label") or "").strip().lower()
    if raw_label in code_map:
        return code_map[raw_label]
    return "gv" if str(order_type or "").strip().lower() == "processing" else "op"


def _serialize_item_from_row(row) -> dict:
    item = {
        "sku": str(row.sku or "").strip(),
        "sku_code": str(row.sku or "").strip(),
        "name": str(row.name or "").strip(),
        "size": str(row.size or "").strip(),
        "barcode": str(row.barcode or "").strip(),
        "qty": int(row.qty or 0),
    }
    marking_code = str(row.marking_code or "").strip()
    if marking_code:
        item["marking_code"] = marking_code
    goods_type = str(row.goods_type or "").strip()
    if goods_type:
        item["goods_type"] = goods_type
    return item


def _serialize_unit_from_row(row) -> dict:
    return {
        "sku": str(row.sku or "").strip(),
        "sku_code": str(row.sku or "").strip(),
        "name": str(row.name or "").strip(),
        "size": str(row.size or "").strip(),
        "barcode": str(row.barcode or "").strip(),
        "marking_code": str(row.marking_code or "").strip(),
        "box_code": str(row.box_code or "").strip(),
        "pallet_code": str(row.pallet_code or "").strip(),
        "qty": int(row.qty or 0),
        "goods_type": str(row.goods_type or "").strip(),
        "location": _location_from_row(row),
        "location_label": str(row.location or "").strip() or _location_label(_location_from_row(row)),
    }


def _row_from_snapshot(snapshot: WarehouseStockSnapshot):
    container = snapshot.container
    parent = snapshot.parent_container
    box_code = ""
    pallet_code = ""
    if container is not None and container.container_type == WarehouseContainer.TYPE_BOX:
        box_code = str(container.container_code or "").strip()
        if parent is not None:
            pallet_code = str(parent.container_code or "").strip()
    elif parent is not None:
        pallet_code = str(parent.container_code or "").strip()
        if container is not None:
            box_code = str(container.container_code or "").strip()
    elif container is not None:
        pallet_code = str(container.container_code or "").strip()
    elif str(snapshot.container_code or "").strip():
        pallet_code = str(snapshot.container_code or "").strip()
    location = snapshot.location
    return SimpleNamespace(
        id=int(snapshot.id or 0),
        agency=snapshot.agency,
        agency_id=int(snapshot.agency_id or 0),
        sku_ref=snapshot.sku_ref,
        sku_ref_id=int(snapshot.sku_ref_id or 0),
        order_type=str(snapshot.source_context_type or "").strip(),
        order_id=str(snapshot.source_context_id or "").strip(),
        sku=str(snapshot.sku_code or "").strip(),
        name=str(snapshot.name or "").strip(),
        size=str(snapshot.size or "").strip(),
        barcode=str(snapshot.barcode or "").strip(),
        marking_code=str(snapshot.marking_code or "").strip(),
        goods_type=str(snapshot.goods_type or "").strip(),
        qty=int(snapshot.qty or 0),
        available_qty=int(snapshot.available_qty or 0),
        processing_reserved_qty=int(snapshot.processing_reserved_qty or 0),
        shipping_reserved_qty=int(snapshot.shipping_reserved_qty or 0),
        box_code=box_code,
        pallet_code=pallet_code,
        zone=str((location.zone_code if location else snapshot.zone_code) or "").strip(),
        row=int(getattr(location, "row_no", 0) or 0),
        section=int(getattr(location, "section_no", 0) or 0),
        tier=int(getattr(location, "tier_no", 0) or 0),
        cell=int(getattr(location, "cell_no", 0) or 0),
        location=str(getattr(location, "display_name", "") or "").strip()
        or _location_label(
            _location_dict(
                zone=str((location.zone_code if location else snapshot.zone_code) or "").strip(),
                row=int(getattr(location, "row_no", 0) or 0),
                section=int(getattr(location, "section_no", 0) or 0),
                tier=int(getattr(location, "tier_no", 0) or 0),
                cell=int(getattr(location, "cell_no", 0) or 0),
            )
        ),
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


@dataclass
class StockPalletTree:
    agency: Agency
    pallet_code: str
    payload: dict
    source_rows: list
    context_order_type: str
    context_order_id: str

    @property
    def synthetic_entry(self):
        return SimpleNamespace(
            order_id=self.context_order_id,
            order_type=self.context_order_type,
            agency=self.agency,
            agency_id=int(self.agency.id or 0),
            payload=self.payload,
        )


class OperationalStockService:
    DEFAULT_ZONE_BY_ORDER = {
        "receiving": "PR",
        "processing": "OBR",
    }

    @staticmethod
    def _warehouse_rows_queryset(
        *,
        agency_id: int | None = None,
        pallet_code: str | None = None,
        pallet_codes: list[str] | tuple[str, ...] | set[str] | None = None,
        box_code: str | None = None,
        include_boxless: bool = True,
    ):
        qs = (
            WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0)
            .select_related("agency", "sku_ref", "container", "parent_container", "location")
            .order_by("container_code", "marking_code", "created_at", "id")
        )
        if agency_id:
            qs = qs.filter(agency_id=int(agency_id))
        target_pallet = str(pallet_code or "").strip()
        target_pallets = {
            str(code or "").strip()
            for code in (pallet_codes or [])
            if str(code or "").strip()
        }
        target_box = str(box_code or "").strip()
        if target_pallet:
            qs = qs.filter(
                Q(parent_container__container_code=target_pallet)
                | Q(container__container_code=target_pallet)
                | Q(container_code=target_pallet)
            )
        if target_pallets:
            qs = qs.filter(
                Q(parent_container__container_code__in=target_pallets)
                | Q(container__container_code__in=target_pallets)
                | Q(container_code__in=target_pallets)
            )
        if target_box:
            qs = qs.filter(container__container_code=target_box)
        if not include_boxless:
            qs = qs.filter(container__isnull=False).exclude(container__container_code="")
        rows = [_row_from_snapshot(snapshot) for snapshot in qs]
        if target_pallet:
            rows = [row for row in rows if str(row.pallet_code or "").strip() == target_pallet]
        if target_pallets:
            rows = [row for row in rows if str(row.pallet_code or "").strip() in target_pallets]
        if target_box:
            rows = [row for row in rows if str(row.box_code or "").strip() == target_box]
        if not include_boxless:
            rows = [row for row in rows if str(row.box_code or "").strip()]
        return sorted(
            rows,
            key=lambda row: (
                str(row.pallet_code or ""),
                str(row.box_code or ""),
                str(row.marking_code or ""),
                row.created_at,
                int(row.id or 0),
            ),
        )

    @staticmethod
    def clear_order_placement(
        agency: Agency | None,
        order_type: str,
        order_id: str | int | None,
        *,
        refresh_keys: bool = True,
    ) -> int:
        if not agency or not order_id:
            return 0
        context_type = str(order_type or "").strip() or "receiving"
        context_id = str(order_id)
        snapshot_qs = WarehouseStockSnapshot.objects.filter(
            agency=agency,
            source_context_type=context_type,
            source_context_id=context_id,
        )
        container_qs = WarehouseContainer.objects.filter(
            agency=agency,
            source_context_type=context_type,
            source_context_id=context_id,
        )
        if context_type == "processing":
            active_reserves = list(
                WarehouseReserve.objects.filter(
                    agency=agency,
                    reserve_type=WarehouseReserve.TYPE_PROCESSING,
                    context_type="processing",
                    context_id=context_id,
                    status__in=[
                        WarehouseReserve.STATUS_ACTIVE,
                        WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                        WarehouseReserve.STATUS_ALLOCATED,
                        WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                        WarehouseReserve.STATUS_SATISFIED,
                        WarehouseReserve.STATUS_RELEASED,
                    ],
                ).order_by("id")
            )
            preserved_snapshot_ids = {
                int(snapshot_id)
                for snapshot_id in WarehouseWritePathService._reserve_snapshot_id_map(active_reserves).values()
                if int(snapshot_id or 0) > 0
            }
            preserved_snapshot_ids.update(
                int(snapshot_id)
                for snapshot_id in snapshot_qs.filter(processing_reserved_qty__gt=0).values_list("id", flat=True)
                if int(snapshot_id or 0) > 0
            )
            if preserved_snapshot_ids:
                preserved_container_ids = {
                    int(container_id)
                    for container_id in snapshot_qs.filter(id__in=preserved_snapshot_ids).values_list("container_id", flat=True)
                    if int(container_id or 0) > 0
                }
                preserved_container_ids.update(
                    int(container_id)
                    for container_id in snapshot_qs.filter(id__in=preserved_snapshot_ids).values_list("parent_container_id", flat=True)
                    if int(container_id or 0) > 0
                )
                snapshot_qs = snapshot_qs.exclude(id__in=preserved_snapshot_ids)
                if preserved_container_ids:
                    container_qs = container_qs.exclude(id__in=preserved_container_ids)
        snapshot_ids = list(snapshot_qs.values_list("id", flat=True))
        container_ids = list(container_qs.values_list("id", flat=True))
        if snapshot_ids:
            WarehouseStockSnapshot.objects.filter(id__in=snapshot_ids).delete()
        if context_type == "processing":
            WarehouseEvent.objects.filter(
                agency=agency,
                source_document_type="processing_placement",
                source_document_id=context_id,
            ).delete()
        else:
            WarehouseEvent.objects.filter(
                agency=agency,
                stock_context_type=context_type,
                stock_context_id=context_id,
            ).delete()
        if container_ids:
            WarehouseContainer.objects.filter(id__in=container_ids).delete()
        return len(snapshot_ids)

    @staticmethod
    @transaction.atomic
    def replace_order_placement(
        agency: Agency | None,
        order_type: str,
        order_id: str | int | None,
        payload: dict | None,
        performed_by=None,
    ) -> int:
        if not agency or not order_id:
            return 0

        order_type = str(order_type or "").strip() or "receiving"
        order_id = str(order_id)
        payload = payload or {}
        default_zone = OperationalStockService.DEFAULT_ZONE_BY_ORDER.get(order_type, "PR")
        goods_type = _goods_type_label(order_type, payload)
        boxes = payload.get("act_boxes") or []
        pallets = payload.get("act_pallets") or []
        act_units = payload.get("act_units") or []
        act_items_removed = bool(payload.get("act_items_removed"))

        box_to_pallet: dict[str, str] = {}
        pallet_locations: dict[str, dict] = {}
        for pallet in pallets if isinstance(pallets, list) else []:
            if not isinstance(pallet, dict):
                continue
            pallet_code = str(pallet.get("code") or "").strip()
            if not pallet_code:
                continue
            pallet_locations[pallet_code] = _container_location(pallet, default_zone)
            for box_code in pallet.get("boxes") or []:
                box_text = str(box_code or "").strip()
                if box_text and box_text not in box_to_pallet:
                    box_to_pallet[box_text] = pallet_code

        prepared_rows: list[dict] = []

        def append_row(
            item: dict | None,
            *,
            box_code: str = "",
            pallet_code: str = "",
            location_override: dict | None = None,
        ) -> None:
            if not isinstance(item, dict):
                return
            sku = str(item.get("sku") or item.get("sku_code") or "").strip()
            name = str(item.get("name") or "").strip()
            size = str(item.get("size") or "").strip()
            barcode = _clean_barcode(item.get("barcode"))
            marking_code = str(item.get("marking_code") or item.get("cz_code") or item.get("code") or "").strip()
            item_goods_type = str(item.get("goods_type") or goods_type or "").strip()
            qty = _parse_qty(item.get("qty") if item.get("qty") is not None else item.get("actual_qty"))
            if qty <= 0 or not any((sku, name, size, barcode, marking_code)):
                return
            location = location_override or (
                pallet_locations.get(pallet_code) if pallet_code else _container_location({}, default_zone)
            )
            prepared_rows.append(
                {
                    "sku": sku,
                    "name": name,
                    "size": size,
                    "barcode": barcode,
                    "marking_code": marking_code,
                    "goods_type": item_goods_type,
                    "qty": qty,
                    "box_code": str(box_code or "").strip(),
                    "pallet_code": str(pallet_code or "").strip(),
                    "location": location,
                }
            )

        if isinstance(act_units, list) and act_units:
            for unit in act_units:
                if not isinstance(unit, dict):
                    continue
                unit_box_code = str(unit.get("box_code") or "").strip()
                unit_pallet_code = str(unit.get("pallet_code") or "").strip()
                location_override = None
                if unit_box_code and not unit_pallet_code:
                    unit_pallet_code = box_to_pallet.get(unit_box_code, "")
                if unit_box_code and not unit_pallet_code:
                    source_box = next(
                        (
                            box
                            for box in boxes
                            if isinstance(box, dict) and str(box.get("code") or "").strip() == unit_box_code
                        ),
                        None,
                    )
                    if source_box:
                        location_override = _container_location(source_box, default_zone)
                append_row(
                    {
                        **unit,
                        "qty": 1,
                    },
                    box_code=unit_box_code,
                    pallet_code=unit_pallet_code,
                    location_override=location_override,
                )
        else:
            for box in boxes if isinstance(boxes, list) else []:
                if not isinstance(box, dict):
                    continue
                box_code = str(box.get("code") or "").strip()
                pallet_code = box_to_pallet.get(box_code, "")
                box_location = None if pallet_code else _container_location(box, default_zone)
                for item in box.get("items") or []:
                    append_row(
                        item,
                        box_code=box_code,
                        pallet_code=pallet_code,
                        location_override=box_location,
                    )
            for pallet in pallets if isinstance(pallets, list) else []:
                if not isinstance(pallet, dict):
                    continue
                pallet_code = str(pallet.get("code") or "").strip()
                for item in pallet.get("items") or []:
                    append_row(item, pallet_code=pallet_code)
            if not prepared_rows and not act_items_removed:
                for item in payload.get("act_items") or []:
                    append_row(item)

        sku_codes = {
            str(row.get("sku") or "").strip()
            for row in prepared_rows
            if str(row.get("sku") or "").strip()
        }
        sku_ref_by_code: dict[str, int] = {}
        barcode_by_sku_size: dict[tuple[str, str], str] = {}
        barcode_by_sku_default: dict[str, str] = {}
        if sku_codes:
            for sku in SKU.objects.filter(agency=agency, deleted=False, sku_code__in=sku_codes).only("id", "sku_code"):
                sku_code = str(sku.sku_code or "").strip().lower()
                if sku_code and sku_code not in sku_ref_by_code:
                    sku_ref_by_code[sku_code] = int(sku.id)
            barcode_qs = (
                SKUBarcode.objects.select_related("sku")
                .filter(
                    sku__agency=agency,
                    sku__deleted=False,
                    sku__sku_code__in=sku_codes,
                )
                .order_by("sku__sku_code", "-is_primary", "id")
            )
            for barcode in barcode_qs:
                value = str(barcode.value or "").strip()
                if not value:
                    continue
                sku_code = str(barcode.sku.sku_code or "").strip().lower()
                if not sku_code:
                    continue
                if sku_code not in barcode_by_sku_default:
                    barcode_by_sku_default[sku_code] = value
                size_key = str(barcode.size or "").strip().lower()
                if size_key and (sku_code, size_key) not in barcode_by_sku_size:
                    barcode_by_sku_size[(sku_code, size_key)] = value

        warehouse_items: list[dict] = []
        for row in prepared_rows:
            sku = str(row.get("sku") or "").strip()
            sku_key = sku.lower()
            size = str(row.get("size") or "").strip()
            barcode = _clean_barcode(row.get("barcode"))
            if not barcode and sku_key:
                barcode = barcode_by_sku_size.get((sku_key, size.lower())) or barcode_by_sku_default.get(sku_key, "")
            warehouse_items.append(
                {
                    "sku": sku,
                    "sku_code": sku,
                    "name": str(row.get("name") or "").strip(),
                    "size": size,
                    "barcode": barcode,
                    "marking_code": str(row.get("marking_code") or "").strip(),
                    "goods_type": str(row.get("goods_type") or goods_type or "").strip(),
                    "qty": int(row.get("qty") or 0),
                    "box_code": str(row.get("box_code") or "").strip(),
                    "pallet_code": str(row.get("pallet_code") or "").strip(),
                    "location": row.get("location") or {},
                }
            )

        OperationalStockService.clear_order_placement(agency, order_type, order_id, refresh_keys=False)
        if warehouse_items:
            warehouse_state_code = ""
            if order_type == "processing" and (
                payload.get("flow_closed") or str(payload.get("act_state") or "").strip() == "closed"
            ):
                warehouse_state_code = WarehouseStateCode.PLACED_AFTER_PROCESSING.value
            WarehouseWritePathService.create_receiving_placement(
                agency=agency,
                order_id=order_id,
                items=warehouse_items,
                performed_by=performed_by,
                source_document_type=f"{order_type}_placement",
                source_document_id=order_id,
                stock_context_type=order_type,
                respect_item_location=True,
                warehouse_state_code=warehouse_state_code,
                receiving_location_code=str(
                    payload.get("receiving_location_code") or ""
                ).strip(),
            )
        WarehouseWritePathService.sync_box_characteristics(
            agency=agency,
            order_id=order_id,
            order_type=order_type,
            boxes=boxes if isinstance(boxes, list) else [],
            performed_by=performed_by,
        )
        return len(warehouse_items)

    @staticmethod
    def get_pallet_tree(
        pallet_code: str,
        *,
        agency_id: int | None = None,
    ) -> StockPalletTree | None:
        target_code = str(pallet_code or "").strip()
        if not target_code:
            return None
        source_rows = list(
            OperationalStockService._warehouse_rows_queryset(
                agency_id=agency_id,
                pallet_code=target_code,
            )
        )
        if not source_rows:
            return None

        agency = source_rows[0].agency
        context_order_type = str(source_rows[0].order_type or "").strip() or "receiving"
        context_order_id = str(source_rows[0].order_id or "").strip()
        pallet_location = _location_from_row(source_rows[0])

        boxes_map: dict[str, dict] = {}
        pallet_items: list[dict] = []
        for row in source_rows:
            item = _serialize_item_from_row(row)
            box_code = str(row.box_code or "").strip()
            if box_code:
                box = boxes_map.setdefault(
                    box_code,
                    {
                        "code": box_code,
                        "items": [],
                    },
                )
                box["items"].append(item)
            else:
                pallet_items.append(item)

        act_boxes = [boxes_map[key] for key in sorted(boxes_map.keys())]
        payload = {
            "act": "placement",
            "act_state": "closed",
            "act_pallets": [
                {
                    "code": target_code,
                    "boxes": [box["code"] for box in act_boxes],
                    "items": pallet_items,
                    "location": pallet_location,
                }
            ],
            "act_boxes": act_boxes,
        }
        return StockPalletTree(
            agency=agency,
            pallet_code=target_code,
            payload=payload,
            source_rows=source_rows,
            context_order_type=context_order_type,
            context_order_id=context_order_id,
        )

    @staticmethod
    def get_pallet_boxes(
        pallet_code: str,
        *,
        agency_id: int | None = None,
    ) -> list[dict]:
        target_code = str(pallet_code or "").strip()
        if not target_code:
            return []
        source_rows = list(
            OperationalStockService._warehouse_rows_queryset(
                agency_id=agency_id,
                pallet_code=target_code,
                include_boxless=False,
            )
        )
        if not source_rows:
            return []

        boxes_map: dict[str, dict] = {}
        for row in source_rows:
            box_code = str(row.box_code or "").strip()
            if not box_code:
                continue
            box = boxes_map.setdefault(
                box_code,
                {
                    "code": box_code,
                    "pallet_code": target_code,
                    "location": _location_from_row(row),
                    "location_label": str(row.location or "").strip() or _location_label(_location_from_row(row)),
                    "qty": 0,
                    "barcode_qty": {},
                    "items": [],
                    "marked_units": [],
                },
            )
            item = _serialize_item_from_row(row)
            box["items"].append(item)
            item_qty = int(row.qty or 0)
            if item_qty > 0:
                box["qty"] += item_qty
            barcode = str(row.barcode or "").strip()
            if barcode and item_qty > 0:
                box["barcode_qty"][barcode] = int(box["barcode_qty"].get(barcode, 0)) + item_qty
            if str(row.marking_code or "").strip():
                box["marked_units"].append(_serialize_unit_from_row(row))

        return [boxes_map[key] for key in sorted(boxes_map.keys())]

    @staticmethod
    def get_pallet_boxes_bulk(
        pallet_codes,
        *,
        agency_id: int | None = None,
    ) -> dict[str, list[dict]]:
        target_codes = sorted(
            {
                str(code or "").strip()
                for code in (pallet_codes or [])
                if str(code or "").strip()
            }
        )
        if not target_codes:
            return {}
        source_rows = OperationalStockService._warehouse_rows_queryset(
            agency_id=agency_id,
            pallet_codes=target_codes,
            include_boxless=False,
        )
        boxes_by_pallet: dict[str, dict[str, dict]] = {
            code: {} for code in target_codes
        }
        for row in source_rows:
            pallet_code = str(row.pallet_code or "").strip()
            box_code = str(row.box_code or "").strip()
            if pallet_code not in boxes_by_pallet or not box_code:
                continue
            boxes_map = boxes_by_pallet[pallet_code]
            box = boxes_map.setdefault(
                box_code,
                {
                    "code": box_code,
                    "pallet_code": pallet_code,
                    "location": _location_from_row(row),
                    "location_label": str(row.location or "").strip() or _location_label(_location_from_row(row)),
                    "qty": 0,
                    "barcode_qty": {},
                    "items": [],
                    "marked_units": [],
                },
            )
            item = _serialize_item_from_row(row)
            box["items"].append(item)
            item_qty = int(row.qty or 0)
            if item_qty > 0:
                box["qty"] += item_qty
            barcode = str(row.barcode or "").strip()
            if barcode and item_qty > 0:
                box["barcode_qty"][barcode] = int(box["barcode_qty"].get(barcode, 0)) + item_qty
            if str(row.marking_code or "").strip():
                box["marked_units"].append(_serialize_unit_from_row(row))
        return {
            pallet_code: [boxes_map[key] for key in sorted(boxes_map)]
            for pallet_code, boxes_map in boxes_by_pallet.items()
        }

    @staticmethod
    def get_box_items(
        box_code: str,
        *,
        agency_id: int | None = None,
        pallet_code: str | None = None,
    ) -> list[dict]:
        target_code = str(box_code or "").strip()
        if not target_code:
            return []
        rows = list(
            OperationalStockService._warehouse_rows_queryset(
                agency_id=agency_id,
                pallet_code=pallet_code,
                box_code=target_code,
            )
        )
        return [_serialize_item_from_row(row) for row in rows]

    @staticmethod
    def get_marked_units(
        *,
        agency_id: int | None = None,
        pallet_code: str | None = None,
        box_code: str | None = None,
        sku: str | None = None,
        barcode: str | None = None,
    ) -> list[dict]:
        rows = OperationalStockService._warehouse_rows_queryset(
            agency_id=agency_id,
            pallet_code=pallet_code,
            box_code=box_code,
        )
        rows = [row for row in rows if str(row.marking_code or "").strip()]
        if sku:
            rows = [row for row in rows if str(row.sku or "").strip() == str(sku or "").strip()]
        if barcode:
            rows = [row for row in rows if str(row.barcode or "").strip() == str(barcode or "").strip()]
        return [_serialize_unit_from_row(row) for row in rows]

    @staticmethod
    @transaction.atomic
    def replace_pallet_tree(tree: StockPalletTree, payload: dict) -> int:
        if not tree.source_rows:
            return 0
        return OperationalStockService.replace_order_placement(
            tree.agency,
            tree.context_order_type,
            tree.context_order_id,
            payload or {},
        )


__all__ = ["OperationalStockService", "StockPalletTree"]
