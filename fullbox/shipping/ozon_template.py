"""Ozon shipping Excel: one upload → N peer ShippingOrders by destination warehouse."""
from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from django.core.files.base import ContentFile
from django.http import FileResponse, HttpResponse
from openpyxl import Workbook, load_workbook

from .client_template import (
    AssembledLine,
    AssembledSkuSummary,
    _allocate_pieces_across_pack_rows,
    _cell_int,
    _norm,
    _norm_barcode,
    _norm_key,
    _rows_for_barcode,
    merge_comment_with_discrepancies,
)

OZON_TEMPLATE_PATH = Path(__file__).resolve().parent / "data" / "shipping_ozon_import_template.xlsx"
OZON_BATCH_MARKER = "[OZON-BATCH:"
OZON_TEMPLATE_HEADERS = (
    "ШК товара",
    "Артикул товара",
    "Кол-во товаров",
    "Склад назначения",
    "ШК ГМ",
    "Тип ГМ (не обязательно)",
)
SESSION_KEY_PREFIX = "shipping_ozon_batch:"


@dataclass
class OzonTemplateLine:
    row_no: int
    barcode: str
    sku: str
    qty: int
    warehouse: str
    gm_barcode: str = ""
    gm_type: str = ""


@dataclass
class OzonWarehouseGroup:
    warehouse: str
    selected_boxes: dict[str, str] = field(default_factory=dict)
    discrepancies: list[str] = field(default_factory=list)
    assembled_lines: list[AssembledLine] = field(default_factory=list)
    assembled_by_sku: list[AssembledSkuSummary] = field(default_factory=list)
    applied_lines: int = 0
    gm_notes: list[str] = field(default_factory=list)
    # Ozon cargo-place barcodes (ШК ГМ) to sticker onto our warehouse boxes.
    gm_bindings: list[dict[str, Any]] = field(default_factory=list)
    boxes_total: int = 0
    qty_total: int = 0

    def gm_barcodes(self) -> list[str]:
        seen: list[str] = []
        for row in self.gm_bindings:
            code = _norm(row.get("gm_barcode"))
            if code and code not in seen:
                seen.append(code)
        return seen

    def gm_by_product_barcode(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for row in self.gm_bindings:
            product = _norm_barcode(row.get("product_barcode")) or _norm(row.get("product_barcode"))
            gm = _norm(row.get("gm_barcode"))
            if not product or not gm:
                continue
            bucket = mapping.setdefault(product, [])
            if gm not in bucket:
                bucket.append(gm)
        return mapping

    def to_dict(self) -> dict[str, Any]:
        return {
            "warehouse": self.warehouse,
            "selected_boxes": dict(self.selected_boxes),
            "discrepancies": list(self.discrepancies),
            "gm_notes": list(self.gm_notes),
            "gm_bindings": list(self.gm_bindings),
            "gm_barcodes": self.gm_barcodes(),
            "boxes_total": int(self.boxes_total),
            "qty_total": int(self.qty_total),
            "applied_lines": int(self.applied_lines),
            "assembled_by_sku": [
                {
                    "sku_code": row.sku_code,
                    "name": row.name,
                    "boxes": int(row.boxes),
                    "qty": int(row.qty),
                }
                for row in self.assembled_by_sku
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OzonWarehouseGroup:
        by_sku = [
            AssembledSkuSummary(
                sku_code=str(row.get("sku_code") or ""),
                name=str(row.get("name") or ""),
                boxes=int(row.get("boxes") or 0),
                qty=int(row.get("qty") or 0),
            )
            for row in (data.get("assembled_by_sku") or [])
        ]
        return cls(
            warehouse=str(data.get("warehouse") or "").strip(),
            selected_boxes={str(k): str(v) for k, v in (data.get("selected_boxes") or {}).items()},
            discrepancies=list(data.get("discrepancies") or []),
            assembled_by_sku=by_sku,
            applied_lines=int(data.get("applied_lines") or 0),
            gm_notes=list(data.get("gm_notes") or []),
            gm_bindings=list(data.get("gm_bindings") or []),
            boxes_total=int(data.get("boxes_total") or 0),
            qty_total=int(data.get("qty_total") or 0),
        )


@dataclass
class OzonMultiImportResult:
    batch_id: str = ""
    groups: list[OzonWarehouseGroup] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    discrepancies: list[str] = field(default_factory=list)
    file_name: str = ""
    file_b64: str = ""

    @property
    def has_discrepancies(self) -> bool:
        return bool(self.discrepancies) or any(g.discrepancies for g in self.groups)

    @property
    def applied_groups(self) -> list[OzonWarehouseGroup]:
        return [g for g in self.groups if g.applied_lines > 0 and g.selected_boxes]

    def all_discrepancies(self) -> list[str]:
        items = list(self.discrepancies)
        for group in self.groups:
            items.extend(group.discrepancies)
        return items

    def to_session_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "file_name": self.file_name,
            "file_b64": self.file_b64,
            "discrepancies": list(self.discrepancies),
            "groups": [g.to_dict() for g in self.groups],
        }

    @classmethod
    def from_session_dict(cls, data: dict[str, Any] | None) -> OzonMultiImportResult | None:
        if not isinstance(data, dict) or not data.get("batch_id"):
            return None
        groups = [OzonWarehouseGroup.from_dict(row) for row in (data.get("groups") or [])]
        return cls(
            batch_id=str(data.get("batch_id") or ""),
            groups=groups,
            discrepancies=list(data.get("discrepancies") or []),
            file_name=str(data.get("file_name") or ""),
            file_b64=str(data.get("file_b64") or ""),
        )


def build_ozon_shipping_template_response() -> HttpResponse:
    if OZON_TEMPLATE_PATH.exists():
        return FileResponse(
            OZON_TEMPLATE_PATH.open("rb"),
            as_attachment=True,
            filename="ozon-shk-excel.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    wb = Workbook()
    ws = wb.active
    ws.title = "Состав ГМ поставки"
    ws.append(list(OZON_TEMPLATE_HEADERS))
    info = wb.create_sheet("Инструкция")
    for line in [
        "1. ШК товара — штрихкод из остатков Fullbox.",
        "2. Кол-во товаров — штуки; система сама наберёт целые короба «до» заявленного.",
        "3. Склад назначения — обязателен; по каждому уникальному складу создаётся отдельная заявка.",
        "4. ШК ГМ и Тип ГМ — для менеджера (в комментарий заявки), складской процесс не меняем.",
        "5. Алиас старого Ozon-заголовка «Зона размещения (склад)» тоже принимается.",
    ]:
        info.append([line])
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    response = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="ozon-shk-excel.xlsx"'
    return response


def _ozon_header_map(header_row: tuple[Any, ...]) -> dict[str, int]:
    aliases = {
        "шк товара": "barcode",
        "баркод товара": "barcode",
        "баркод": "barcode",
        "штрихкод": "barcode",
        "штрихкод товара": "barcode",
        "barcode": "barcode",
        "артикул товара": "sku",
        "артикул": "sku",
        "sku": "sku",
        "кол-во товаров": "qty",
        "кол во товаров": "qty",
        "количество товаров": "qty",
        "количество": "qty",
        "qty": "qty",
        "склад назначения": "warehouse",
        "зона размещения (склад)": "warehouse",
        "зона размещения": "warehouse",
        "склад": "warehouse",
        "destination warehouse": "warehouse",
        "шк гм": "gm_barcode",
        "штрихкод гм": "gm_barcode",
        "тип гм (не обязательно)": "gm_type",
        "тип гм": "gm_type",
    }
    mapping: dict[str, int] = {}
    for idx, raw in enumerate(header_row):
        key = aliases.get(_norm_key(raw))
        if key and key not in mapping:
            mapping[key] = idx
    return mapping


def parse_ozon_shipping_template(file_obj) -> tuple[list[OzonTemplateLine], list[str]]:
    errors: list[str] = []
    try:
        if hasattr(file_obj, "seek"):
            try:
                file_obj.seek(0)
            except Exception:
                pass
        wb = load_workbook(file_obj, data_only=True, read_only=True)
    except Exception:
        return [], ["Не удалось прочитать Excel Ozon. Загрузите файл по шаблону."]

    # Prefer sheet with Ozon columns; else first sheet.
    ws = wb.active
    for name in wb.sheetnames:
        if "гм" in str(name).casefold() or "состав" in str(name).casefold():
            ws = wb[name]
            break

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], ["Файл пустой."]
    header_map = _ozon_header_map(tuple(rows[0]))
    if "barcode" not in header_map:
        return [], ["В первой строке нужен столбец «ШК товара»."]
    if "qty" not in header_map:
        return [], ["Нужен столбец «Кол-во товаров»."]
    if "warehouse" not in header_map:
        return [], ["Нужен столбец «Склад назначения»."]

    lines: list[OzonTemplateLine] = []
    for row_no, row in enumerate(rows[1:], start=2):
        if not row or all(cell is None or str(cell).strip() == "" for cell in row):
            continue

        def get(name: str) -> Any:
            idx = header_map.get(name)
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        barcode = _norm_barcode(get("barcode"))
        if not barcode:
            continue
        warehouse = _norm(get("warehouse"))
        if not warehouse:
            errors.append(f"Строка {row_no}: укажите «Склад назначения».")
            continue
        try:
            qty = _cell_int(get("qty"))
        except ValueError:
            errors.append(f"Строка {row_no}: количество должно быть целым числом.")
            continue
        if qty is None or qty <= 0:
            errors.append(f"Строка {row_no}: количество должно быть > 0.")
            continue
        lines.append(
            OzonTemplateLine(
                row_no=row_no,
                barcode=barcode,
                sku=_norm(get("sku")),
                qty=int(qty),
                warehouse=warehouse,
                gm_barcode=_norm_barcode(get("gm_barcode")) or _norm(get("gm_barcode")),
                gm_type=_norm(get("gm_type")),
            )
        )

    if not lines and not errors:
        errors.append("В файле нет строк с ШК товара и складом назначения.")
    return lines, errors


def _finalize_group(group: OzonWarehouseGroup, stock_by_key: dict[str, dict]) -> None:
    by_sku: dict[str, AssembledSkuSummary] = {}
    boxes_total = 0
    qty_total = 0
    for line in group.assembled_lines:
        boxes_total += int(line.boxes)
        qty_total += int(line.applied_qty)
        sku = _norm(line.sku_code) or "-"
        current = by_sku.get(sku)
        if current is None:
            by_sku[sku] = AssembledSkuSummary(
                sku_code=sku,
                name=_norm(line.name),
                boxes=int(line.boxes),
                qty=int(line.applied_qty),
            )
        else:
            current.boxes += int(line.boxes)
            current.qty += int(line.applied_qty)
            if not current.name and line.name:
                current.name = _norm(line.name)
    group.assembled_by_sku = sorted(by_sku.values(), key=lambda item: item.sku_code.casefold())
    group.boxes_total = boxes_total
    group.qty_total = qty_total
    # Cap against stock availability across keys (safety).
    for key, boxes_s in list(group.selected_boxes.items()):
        row = stock_by_key.get(key)
        if row is None:
            continue
        available = max(int(row.get("available_boxes") or 0), 0)
        boxes = int(boxes_s or 0)
        if boxes > available:
            group.discrepancies.append(
                f"{row.get('sku_code')}/{row.get('size') or '-'} "
                f"(склад {group.warehouse}): суммарно {boxes} кор., доступно {available}."
            )
            group.selected_boxes[key] = str(available)


def apply_ozon_shipping_template_by_warehouse(
    *,
    file_obj,
    stock_rows: list[dict],
    file_name: str = "",
) -> OzonMultiImportResult:
    result = OzonMultiImportResult(batch_id=uuid.uuid4().hex[:12], file_name=file_name or "")
    raw_bytes = b""
    try:
        if hasattr(file_obj, "seek"):
            try:
                file_obj.seek(0)
            except Exception:
                pass
        raw_bytes = file_obj.read() if hasattr(file_obj, "read") else b""
        if hasattr(file_obj, "seek"):
            try:
                file_obj.seek(0)
            except Exception:
                pass
    except Exception:
        raw_bytes = b""
    if raw_bytes:
        # Keep size bounded for session storage (~1.5MB decoded).
        if len(raw_bytes) <= 1_500_000:
            result.file_b64 = base64.b64encode(raw_bytes).decode("ascii")
        parse_source: Any = BytesIO(raw_bytes)
    else:
        parse_source = file_obj

    lines, parse_errors = parse_ozon_shipping_template(parse_source)
    result.parse_errors = parse_errors
    if parse_errors:
        return result

    stock_by_key = {str(row.get("key") or ""): row for row in stock_rows or []}
    # Preserve first-seen warehouse order.
    warehouse_order: list[str] = []
    by_wh: dict[str, list[OzonTemplateLine]] = {}
    for line in lines:
        if line.warehouse not in by_wh:
            by_wh[line.warehouse] = []
            warehouse_order.append(line.warehouse)
        by_wh[line.warehouse].append(line)

    # Shared remaining stock across all warehouses (no double booking).
    already_selected: dict[str, int] = {}

    for warehouse in warehouse_order:
        group = OzonWarehouseGroup(warehouse=warehouse)
        for line in by_wh[warehouse]:
            note_parts = [f"стр.{line.row_no}", f"ШК {line.barcode}", f"{line.qty} шт"]
            if line.sku:
                note_parts.append(f"арт. {line.sku}")
            if line.gm_barcode:
                note_parts.append(f"ГМ {line.gm_barcode}")
            if line.gm_type:
                note_parts.append(f"тип {line.gm_type}")
            group.gm_notes.append(", ".join(note_parts))

            pool = _rows_for_barcode(line.barcode, stock_rows)
            if not pool:
                group.discrepancies.append(
                    f"Строка {line.row_no} [{warehouse}]: ШК {line.barcode} не найден в доступных остатках."
                )
                continue

            addition, discrepancies, applied = _allocate_pieces_across_pack_rows(
                barcode=line.barcode,
                qty=line.qty,
                pool=pool,
                row_no=line.row_no,
                already_selected=already_selected,
            )
            # Prefix warehouse in discrepancy text for clarity.
            for item in discrepancies:
                if f"[{warehouse}]" in item:
                    group.discrepancies.append(item)
                else:
                    group.discrepancies.append(item.replace(f"Строка {line.row_no}:", f"Строка {line.row_no} [{warehouse}]:", 1))

            line_boxes = 0
            line_qty = 0
            for key, boxes in addition.items():
                already_selected[key] = int(already_selected.get(key, 0)) + int(boxes)
                group.selected_boxes[key] = str(
                    int(group.selected_boxes.get(key, "0") or 0) + int(boxes)
                )
                row = stock_by_key.get(key) or {}
                box_qty = max(int(row.get("box_qty") or 0), 0)
                applied_qty = int(boxes) * box_qty
                line_boxes += int(boxes)
                line_qty += applied_qty
                group.assembled_lines.append(
                    AssembledLine(
                        row_no=line.row_no,
                        sku_code=_norm(row.get("sku_code")),
                        name=_norm(row.get("name")),
                        size=_norm(row.get("size")),
                        barcode=_norm_barcode(row.get("barcode")) or line.barcode,
                        requested_qty=int(line.qty),
                        applied_qty=applied_qty,
                        boxes=int(boxes),
                        box_qty=box_qty,
                    )
                )
            if line.gm_barcode or applied:
                group.gm_bindings.append(
                    {
                        "row_no": line.row_no,
                        "gm_barcode": line.gm_barcode,
                        "gm_type": line.gm_type,
                        "product_barcode": line.barcode,
                        "sku": line.sku,
                        "requested_qty": int(line.qty),
                        "applied_qty": int(line_qty),
                        "boxes": int(line_boxes),
                    }
                )
            group.applied_lines += applied

        _finalize_group(group, stock_by_key)
        result.groups.append(group)

    result.discrepancies = result.all_discrepancies()
    return result


def items_from_selected_boxes(
    stock_rows: list[dict],
    selected_boxes: dict[str, str | int],
    *,
    gm_by_product_barcode: dict[str, list[str]] | None = None,
) -> tuple[list[dict], int, list[str]]:
    """Build ShippingOrderItem kwargs from picker keys (whole boxes only)."""
    stock_by_key = {str(row.get("key") or ""): row for row in stock_rows or []}
    gm_map = gm_by_product_barcode or {}
    selected_map: dict[tuple, dict] = {}
    total_boxes = 0
    errors: list[str] = []
    for key, boxes_raw in (selected_boxes or {}).items():
        key = str(key or "").strip()
        try:
            boxes = int(boxes_raw)
        except (TypeError, ValueError):
            boxes = 0
        if not key or boxes <= 0:
            continue
        row = stock_by_key.get(key)
        if row is None:
            errors.append(f"Позиция остатка не найдена: {key}")
            continue
        available = max(int(row.get("available_boxes") or 0), 0)
        if boxes > available:
            errors.append(
                f"{row.get('sku_code')}/{row.get('size') or '-'}: доступно только {available} кор."
            )
            boxes = available
        if boxes <= 0:
            continue
        box_qty = max(int(row.get("box_qty") or 0), 0)
        if box_qty <= 0:
            errors.append(f"{row.get('sku_code')}: некорректная кратность короба.")
            continue
        qty_requested = boxes * box_qty
        total_boxes += boxes
        barcode = str(row.get("barcode") or "").strip()
        item_key = (
            str(row.get("sku_code") or "").strip(),
            str(row.get("name") or "").strip(),
            str(row.get("size") or "").strip(),
            barcode,
            str(row.get("goods_type") or "").strip(),
            str(box_qty),
        )
        gm_codes = gm_map.get(_norm_barcode(barcode) or barcode) or gm_map.get(barcode) or []
        gm_suffix = f"; ШК ГМ Ozon: {', '.join(gm_codes)}" if gm_codes else ""
        existing = selected_map.get(item_key)
        comment = f"Коробов: {boxes}; кратность: {box_qty}{gm_suffix}"
        if existing:
            existing["qty_requested"] += qty_requested
            box_total = existing["qty_requested"] // box_qty
            existing_gm = ""
            if "ШК ГМ Ozon:" in str(existing.get("comment") or ""):
                existing_gm = "; " + str(existing["comment"]).split("ШК ГМ Ozon:", 1)[1].strip()
                existing_gm = "ШК ГМ Ozon: " + existing_gm.lstrip("; ").strip()
                existing_gm = f"; {existing_gm}" if existing_gm else ""
            elif gm_suffix:
                existing_gm = gm_suffix
            existing["comment"] = f"Коробов: {box_total}; кратность: {box_qty}{existing_gm}"
            continue
        selected_map[item_key] = {
            "sku_code": item_key[0],
            "name": item_key[1],
            "size": item_key[2],
            "barcode": item_key[3],
            "goods_type": item_key[4],
            "qty_requested": qty_requested,
            "comment": comment,
        }
    selected = list(selected_map.values())
    if not selected:
        errors.append("Нет позиций для склада назначения.")
    return selected, total_boxes, errors


def _strip_ozon_comment_base(user_comment: str) -> str:
    base = str(user_comment or "").strip()
    if not base:
        return ""
    for marker in (OZON_BATCH_MARKER, "[РАСХОЖДЕНИЯ ШАБЛОНА]", "Короба Ozon (ШК ГМ"):
        if marker in base:
            base = base.split(marker, 1)[0].rstrip()
    return base


def build_ozon_parent_comment(
    *,
    user_comment: str,
    batch_id: str,
    warehouses: list[str],
    groups: list[OzonWarehouseGroup],
) -> str:
    parts: list[str] = []
    base = _strip_ozon_comment_base(user_comment)
    if base:
        parts.append(base)
    parts.append(f"{OZON_BATCH_MARKER}{batch_id}]")
    parts.append(
        "Пакет Ozon: одна заявка с распределением по конечным складам "
        "(отдельные складские заявки не создаются)."
    )
    parts.append("Склады назначения: " + ", ".join(warehouses))
    for group in groups:
        parts.append(
            f"- {group.warehouse}: {group.boxes_total} кор. / {group.qty_total} шт"
            + (f", ШК ГМ: {', '.join(group.gm_barcodes())}" if group.gm_barcodes() else "")
        )
    return "\n".join(parts)


def build_ozon_order_comment(
    *,
    user_comment: str,
    batch_id: str,
    warehouse: str,
    group: OzonWarehouseGroup,
    sibling_warehouses: list[str],
    parent_number: str = "",
) -> str:
    parts: list[str] = []
    base = _strip_ozon_comment_base(user_comment)
    if base:
        parts.append(base)
    parts.append(f"{OZON_BATCH_MARKER}{batch_id}]")
    parts.append("Направление пакета Ozon по складу назначения.")
    if parent_number:
        parts.append(f"Родительская заявка пакета: {parent_number}")
    parts.append(f"Склад назначения: {warehouse}")
    if sibling_warehouses:
        others = [w for w in sibling_warehouses if w != warehouse]
        if others:
            parts.append("Другие склады пакета: " + ", ".join(others))
    parts.append(f"Собрано: {group.boxes_total} кор. / {group.qty_total} шт.")
    if group.gm_bindings:
        parts.append("Короба Ozon (ШК ГМ — клеить на наши короба):")
        for row in group.gm_bindings[:100]:
            gm = _norm(row.get("gm_barcode")) or "—"
            product = _norm(row.get("product_barcode")) or "—"
            sku = _norm(row.get("sku")) or "—"
            boxes = int(row.get("boxes") or 0)
            qty = int(row.get("applied_qty") or 0)
            gm_type = _norm(row.get("gm_type"))
            line = f"- ГМ {gm} → товар ШК {product} ({sku}), {boxes} кор. / {qty} шт"
            if gm_type:
                line += f", тип {gm_type}"
            parts.append(line)
        if len(group.gm_bindings) > 100:
            parts.append(f"- … ещё {len(group.gm_bindings) - 100} строк")
    elif group.gm_notes:
        parts.append("Состав ГМ (из шаблона):")
        parts.extend(f"- {note}" for note in group.gm_notes[:80])
    block = ""
    if group.discrepancies:
        block = "[РАСХОЖДЕНИЯ ШАБЛОНА]\nТребуется проверка менеджером:\n" + "\n".join(
            f"- {item}" for item in group.discrepancies
        )
    text = "\n".join(parts)
    return merge_comment_with_discrepancies(text, block)


def ozon_meta_from_comment(comment: str) -> dict[str, Any]:
    """Parse Ozon batch marker / GM lines from order comment for LK UI."""
    text = str(comment or "")
    meta: dict[str, Any] = {
        "is_ozon_batch": OZON_BATCH_MARKER in text,
        "batch_id": "",
        "gm_lines": [],
        "sibling_warehouses": "",
    }
    if not meta["is_ozon_batch"]:
        return meta
    try:
        after = text.split(OZON_BATCH_MARKER, 1)[1]
        meta["batch_id"] = after.split("]", 1)[0].strip()
    except Exception:
        meta["batch_id"] = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Пакет Ozon"):
            meta["sibling_warehouses"] = line.split(":", 1)[-1].strip()
        if line.startswith("- ГМ ") or line.startswith("- ГМ"):
            meta["gm_lines"].append(line.lstrip("- ").strip())
    return meta


def session_key_for_agency(agency_id: int) -> str:
    return f"{SESSION_KEY_PREFIX}{int(agency_id)}"


def attachment_content_file(result: OzonMultiImportResult) -> ContentFile | None:
    if not result.file_b64:
        return None
    try:
        raw = base64.b64decode(result.file_b64.encode("ascii"))
    except Exception:
        return None
    name = result.file_name or f"ozon-batch-{result.batch_id}.xlsx"
    return ContentFile(raw, name=name)
