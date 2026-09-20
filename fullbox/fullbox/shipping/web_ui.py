from __future__ import annotations

"""Shipping UI helpers and request handlers."""

from collections import defaultdict
import json
import logging
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_http_methods

from audit.models import log_order_action
from employees.access import resolve_cabinet_url
from fullbox.container_codes import issue_container_code, issue_pallet_code, normalize_container_kind
from fullbox.order_numbers import format_order_number
from labels.utils import build_print_status_snapshot, refresh_print_agent_printers
from sklad.services.stock_availability import StockAvailabilityService
from sku.models import Agency

from .actions import ShippingDetailActionPermissions, handle_shipping_detail_action
from .box_splits import encode_partial_box_split, extract_partial_box_split
from .dispatch import (
    shipping_dispatch_stage as _shipping_dispatch_stage,
    sign_dispatch_act_logistician,
    sign_dispatch_act_manager,
)
from .forms import (
    NEXT_DAY_DEADLINE_ERROR,
    NEXT_DAY_DEADLINE_HOUR,
    ShippingTransportNoteForm,
    WORKDAY_END_HOUR,
    WORKDAY_HOURS_ERROR,
    WORKDAY_START_HOUR,
    ShippingOrderForm,
)
from .marketplace_warehouses import load_marketplace_warehouse_catalog
from .models import ShippingOrder, ShippingOrderAttachment, ShippingOrderItem
from .packing import (
    _shipping_box_row_key,
    _shipping_boxes_from_packing_payload,
    _shipping_delivered_boxes,
    _shipping_loose_packing_initial_state,
    _shipping_manageable_packing_boxes,
    _shipping_packing_initial_state,
    _shipping_packing_summary,
    request_shipping_packing_pallet_removal,
    resolve_shipping_packing_pallet_removal,
    shipping_packing_pending_pallet_removal,
    shipping_packing_slip_meta,
    shipping_packing_slips_data,
    save_shipping_loose_packing,
    save_shipping_packing,
)
from .return_act import render_return_act_doc, return_act_doc_filename
from .selectors import (
    active_shipping_attachments as _active_shipping_attachments,
    build_shipping_detail_context,
    build_shipping_dispatch_context,
    shipping_attachment_names as _shipping_attachment_names,
    shipping_reachtruck_metrics as _shipping_reachtruck_metrics,
    shipping_ui_status_label as _shipping_ui_status_label,
)
from .services import (
    build_shipping_return_act_response,
    build_shipping_transport_note_docx_response,
    build_shipping_transport_note_pdf_response,
    build_shipping_detail_page_context,
    build_shipping_list_page_context,
    download_shipping_attachment,
    ensure_manager_review_task,
    handle_shipping_dispatch_act_request,
    handle_shipping_dispatch_sign_logistician_request,
    handle_shipping_dispatch_sign_manager_request,
    handle_shipping_create_request,
    handle_shipping_documents_request,
    handle_shipping_loose_packing_request,
    handle_shipping_packing_request,
    handle_shipping_packing_pallet_removal_request,
    handle_shipping_packing_pallet_removal_review_request,
    handle_shipping_packing_slips_request,
    handle_shipping_packing_slips_status_request,
    next_shipping_number,
    order_payload,
    reserve_order,
    shipping_pick_readiness,
    shipping_available_items,
    shipping_selectable_warehouse_state_codes,
)
from .transport_note import (
    build_transport_note_preview_context,
    can_access_transport_note,
    get_or_create_transport_note,
    render_transport_note_docx,
    render_transport_note_pdf,
    transport_note_docx_filename,
    transport_note_filename,
)
from .workflow import (
    can_access_order as _can_access_order,
    can_cancel as _can_cancel,
    can_edit_items as _can_edit_items,
    can_edit_order_form as _can_edit_order_form,
    can_manager_approve as _can_manager_approve,
    can_manager_reopen as _can_manager_reopen,
    can_storekeeper_manage_packing,
    can_storekeeper_accept as _can_storekeeper_accept,
    can_storekeeper_pack as _can_storekeeper_pack,
    can_storekeeper_pick as _can_storekeeper_pick,
    can_submit_for_approval as _can_submit_for_approval,
    can_write as _can_write,
    is_logistician_role as _is_logistician_role,
    is_manager_role as _is_manager_role,
    is_storekeeper_role as _is_storekeeper_role,
    request_scope as _request_scope,
)

_BOX_COUNT_COMMENT_RE = re.compile(r"коробов:\s*(\d+)", re.IGNORECASE)
_BOX_CODES_COMMENT_RE = re.compile(r"короба:\s*([^;]+)", re.IGNORECASE)
logger = logging.getLogger(__name__)


def _to_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _parse_box_count_from_comment(comment: str | None) -> int:
    match = _BOX_COUNT_COMMENT_RE.search(str(comment or ""))
    if not match:
        return 0
    return _to_int(match.group(1))


def _parse_box_codes_from_comment(comment: str | None) -> list[str]:
    match = _BOX_CODES_COMMENT_RE.search(str(comment or ""))
    if not match:
        return []
    codes: list[str] = []
    seen: set[str] = set()
    for raw_code in match.group(1).split(","):
        code = str(raw_code or "").strip()
        normalized = code.lower()
        if not code or normalized in seen:
            continue
        seen.add(normalized)
        codes.append(code)
    return codes


def _display_shipping_number(number: str | None) -> str:
    return format_order_number("shipping", number)


def _stock_row_identity(*, sku_code: str, name: str, size: str, barcode: str, goods_type: str) -> tuple[str, str, str, str, str]:
    return (
        str(sku_code or "").strip(),
        str(name or "").strip(),
        str(size or "").strip(),
        str(barcode or "").strip(),
        str(goods_type or "").strip(),
    )


def _selected_box_values_from_request(request) -> dict[str, str]:
    all_keys = request.POST.getlist("stock_key_all[]") or request.POST.getlist("stock_key_all")
    all_boxes = request.POST.getlist("stock_boxes[]") or request.POST.getlist("stock_boxes")
    values: dict[str, str] = {}
    for index, raw_key in enumerate(all_keys):
        key = str(raw_key or "").strip()
        if not key or key in values:
            continue
        values[key] = str(all_boxes[index] if index < len(all_boxes) else "").strip()
    return values


def _selected_box_values_for_order(order: ShippingOrder, stock_rows: list[dict]) -> dict[str, str]:
    rows_by_identity: dict[tuple[str, str, str, str, str], list[dict]] = defaultdict(list)
    for row in stock_rows:
        rows_by_identity[
            _stock_row_identity(
                sku_code=row.get("sku_code") or "",
                name=row.get("name") or "",
                size=row.get("size") or "",
                barcode=row.get("barcode") or "",
                goods_type=row.get("goods_type") or "",
            )
        ].append(row)

    selected_by_key: dict[str, str] = {}
    mixed_group_boxes: dict[str, int] = {}
    for item in order.items.order_by("id"):
        if extract_partial_box_split(item.comment):
            continue
        parsed_box_codes = _parse_box_codes_from_comment(item.comment)
        matches = rows_by_identity.get(
            _stock_row_identity(
                sku_code=item.sku_code,
                name=item.name,
                size=item.size,
                barcode=item.barcode,
                goods_type=item.goods_type,
            ),
            [],
        )
        if not matches:
            continue
        parsed_boxes = _parse_box_count_from_comment(item.comment)
        selected_row = None
        if parsed_box_codes:
            parsed_box_code_set = {code.lower() for code in parsed_box_codes}
            for row in matches:
                row_codes = {str(code or "").strip().lower() for code in row.get("box_codes") or []}
                if parsed_box_code_set and parsed_box_code_set.issubset(row_codes):
                    parsed_boxes = len(parsed_box_codes)
                    selected_row = row
                    break
        for row in matches:
            if selected_row is not None:
                break
            box_qty = int(row.get("box_qty") or 0)
            if box_qty <= 0:
                continue
            candidate_boxes = parsed_boxes
            if candidate_boxes <= 0 and int(item.qty_requested or 0) % box_qty == 0:
                candidate_boxes = int(item.qty_requested or 0) // box_qty
            if candidate_boxes <= 0:
                continue
            if candidate_boxes * box_qty == int(item.qty_requested or 0):
                parsed_boxes = candidate_boxes
                selected_row = row
                break
            if selected_row is None:
                parsed_boxes = candidate_boxes
                selected_row = row
        if selected_row is None or parsed_boxes <= 0:
            continue
        mixed_group = str(selected_row.get("mixed_group") or "").strip()
        if selected_row.get("is_mixed_box") and mixed_group:
            mixed_group_boxes[mixed_group] = max(int(mixed_group_boxes.get(mixed_group) or 0), int(parsed_boxes))
        else:
            selected_by_key[str(selected_row["key"])] = str(int(parsed_boxes))

    for row in stock_rows:
        mixed_group = str(row.get("mixed_group") or "").strip()
        if row.get("is_mixed_box") and mixed_group and mixed_group in mixed_group_boxes:
            selected_by_key[str(row["key"])] = str(int(mixed_group_boxes[mixed_group]))
    return selected_by_key


def _partial_box_splits_from_request(request) -> list[dict]:
    raw = str(request.POST.get("stock_partial_box_splits_json") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _partial_box_splits_json_from_request(request) -> str:
    splits = _partial_box_splits_from_request(request)
    return json.dumps(splits, ensure_ascii=False, separators=(",", ":")) if splits else "[]"


def _partial_box_count_from_request(request) -> int:
    return sum(max(_to_int(split.get("boxes")), 0) for split in _partial_box_splits_from_request(request))


def _partial_box_counts_from_request(
    request,
    stock_rows: list[dict],
) -> tuple[dict[str, int], dict[str, int]]:
    stock_map = {str(row.get("key") or "").strip(): row for row in stock_rows if str(row.get("key") or "").strip()}
    counts_by_key: dict[str, int] = defaultdict(int)
    counts_by_group: dict[str, int] = defaultdict(int)
    for split in _partial_box_splits_from_request(request):
        row_key = str(split.get("row_key") or "").strip()
        row = stock_map.get(row_key)
        boxes = max(_to_int(split.get("boxes")), 0)
        if row is None or boxes <= 0:
            continue
        group = str(row.get("mixed_group") or "").strip()
        if row.get("is_mixed_box") and group:
            counts_by_group[group] += boxes
        else:
            counts_by_key[row_key] += boxes
    return counts_by_key, counts_by_group


def _partial_box_splits_for_order(order: ShippingOrder, stock_rows: list[dict]) -> list[dict]:
    valid_keys = {str(row.get("key") or "").strip() for row in stock_rows}
    grouped: dict[str, dict] = {}
    for item in order.items.order_by("id"):
        meta = extract_partial_box_split(item.comment)
        if not meta:
            continue
        group_key = str(meta.get("group_key") or meta.get("row_key") or f"item:{item.id}").strip()
        row_key = str(meta.get("row_key") or "").strip()
        if row_key and valid_keys and row_key not in valid_keys:
            continue
        entry = grouped.setdefault(
            group_key,
            {
                "group_key": group_key,
                "row_key": row_key,
                "group": str(meta.get("source_group") or "").strip(),
                "boxes": max(_to_int(meta.get("source_boxes")), 0),
                "items": [],
            },
        )
        if not entry.get("row_key") and row_key:
            entry["row_key"] = row_key
        if not entry.get("group") and meta.get("source_group"):
            entry["group"] = str(meta.get("source_group") or "").strip()
        entry["boxes"] = max(_to_int(entry.get("boxes")), _to_int(meta.get("source_boxes")))
        pick_qty = max(_to_int(meta.get("item_pick_qty")), 0)
        if row_key and pick_qty > 0:
            entry["items"].append({"key": row_key, "qty": pick_qty})
    result: list[dict] = []
    for entry in grouped.values():
        boxes = _to_int(entry.get("boxes"))
        items = [
            {"key": str(item.get("key") or "").strip(), "qty": max(_to_int(item.get("qty")), 0)}
            for item in entry.get("items") or []
            if str(item.get("key") or "").strip() and _to_int(item.get("qty")) > 0
        ]
        if boxes <= 0 or not items:
            continue
        entry["boxes"] = boxes
        entry["items"] = items
        result.append(entry)
    return result


def _partial_box_splits_json_for_order(order: ShippingOrder, stock_rows: list[dict]) -> str:
    splits = _partial_box_splits_for_order(order, stock_rows)
    return json.dumps(splits, ensure_ascii=False, separators=(",", ":")) if splits else "[]"


def _with_selected_boxes(stock_rows: list[dict], selected_boxes: dict[str, str] | None = None) -> list[dict]:
    selected_boxes = selected_boxes or {}
    prepared: list[dict] = []
    for row in stock_rows:
        cloned = dict(row)
        selected_value = selected_boxes.get(str(row.get("key") or ""), "0")
        cloned["selected_boxes"] = str(selected_value or "0")
        prepared.append(cloned)
    return prepared


def _order_box_count(order: ShippingOrder) -> int:
    expected = int(order.expected_boxes or 0)
    if expected > 0:
        return expected
    return sum(_parse_box_count_from_comment(item.comment) for item in order.items.all())


def _can_storekeeper_manage_packing(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    if order.status == ShippingOrder.STATUS_PACKED:
        return can_storekeeper_manage_packing(
            scope,
            role,
            order,
            has_manageable_boxes=True,
        )
    manageable_boxes = _shipping_manageable_packing_boxes(order)
    has_manageable_boxes = bool(manageable_boxes)
    if order.status != ShippingOrder.STATUS_PACKED:
        expected_boxes = _order_box_count(order)
        has_manageable_boxes = expected_boxes > 0 and len(manageable_boxes) >= expected_boxes
    return can_storekeeper_manage_packing(
        scope,
        role,
        order,
        has_manageable_boxes=has_manageable_boxes,
    )


def _save_shipping_attachments(order: ShippingOrder, request) -> list[str]:
    uploaded_names: list[str] = []
    for uploaded_file in request.FILES.getlist("documents"):
        if not uploaded_file or not getattr(uploaded_file, "name", ""):
            continue
        attachment = ShippingOrderAttachment.objects.create(
            order=order,
            uploaded_by=request.user if request.user.is_authenticated else None,
            file=uploaded_file,
        )
        uploaded_names.append(attachment.filename)
    return uploaded_names


def _log_update(order: ShippingOrder, request, description: str, *, action: str = "update", extra: dict | None = None) -> None:
    log_order_action(
        action=action,
        order_id=order.number,
        order_type="shipping",
        user=request.user if request.user.is_authenticated else None,
        agency=order.agency,
        description=description,
        payload=order_payload(order, extra=extra),
    )


def _compose_picker_key(
    *,
    sku_id: int,
    sku_code: str,
    name: str,
    size: str,
    goods_type: str,
    box_qty: int,
    barcode: str,
    mixed_group: str,
) -> str:
    return "|".join(
        [
            str(int(sku_id or 0)),
            str(sku_code or "").strip(),
            str(size or "").strip(),
            str(goods_type or "").strip(),
            str(int(box_qty or 0)),
            str(barcode or "").strip(),
            str(name or "").strip(),
            str(mixed_group or "").strip(),
        ]
    )


def _shipping_stock_picker_rows(
    agency: Agency | None,
    *,
    exclude_order: ShippingOrder | None = None,
) -> list[dict]:
    if not agency:
        return []
    rows = StockAvailabilityService.stock_rows_with_availability(
        agency=agency,
        require_box=True,
        exclude_shipping_order_id=str(exclude_order.number or "").strip() if exclude_order else None,
    )
    allowed_state_codes = set(
        shipping_selectable_warehouse_state_codes(include_reserved=exclude_order is not None)
    )
    rows = [
        row
        for row in rows
        if str(row.get("warehouse_state_code") or "").strip() in allowed_state_codes
    ]

    box_lines: dict[str, dict[tuple[int, str, str, str, str, str], int]] = defaultdict(lambda: defaultdict(int))
    box_available_lines: dict[str, dict[tuple[int, str, str, str, str, str], int]] = defaultdict(lambda: defaultdict(int))
    box_barcodes: dict[str, set[str]] = defaultdict(set)
    box_item_signatures: dict[str, set[tuple[str, str, str, str, str]]] = defaultdict(set)
    box_codes_by_id: dict[str, str] = {}
    for row in rows:
        sku_code = str(row.get("sku") or "").strip()
        if not sku_code:
            continue
        box_code = str(row.get("box_code") or "").strip()
        if not box_code:
            continue
        qty_in_box = int(row.get("qty") or 0)
        if qty_in_box <= 0:
            continue
        size = str(row.get("size") or "").strip()
        goods_type = str(row.get("goods_type") or "").strip()
        barcode = str(row.get("barcode") or "").strip()
        name = str(row.get("name") or "").strip()
        sku_id = int(row.get("sku_ref_id") or 0)
        line_key = (sku_id, sku_code, name, size, barcode, goods_type)
        box_id = f"{int(row.get('agency_id') or 0)}:{box_code}"
        box_codes_by_id[box_id] = box_code
        box_lines[box_id][line_key] += qty_in_box
        box_available_lines[box_id][line_key] += max(int(row.get("available_qty") or 0), 0)
        item_signature = (
            sku_code.lower(),
            size.lower(),
            name.lower(),
            goods_type.lower(),
            barcode.lower(),
        )
        box_item_signatures[box_id].add(item_signature)
        normalized_barcode = barcode.lower()
        if normalized_barcode:
            box_barcodes[box_id].add(normalized_barcode)

    all_box_ids = set(box_item_signatures.keys()) | set(box_barcodes.keys())
    mixed_box_ids: set[str] = set()
    for box_id in all_box_ids:
        barcodes = box_barcodes.get(box_id, set())
        if len(barcodes) >= 2:
            mixed_box_ids.add(box_id)
            continue
        signatures = box_item_signatures.get(box_id, set())
        if len(signatures) >= 2:
            mixed_box_ids.add(box_id)

    fully_available_box_ids = {
        box_id
        for box_id, lines_map in box_lines.items()
        if lines_map
        and all(
            int(box_available_lines.get(box_id, {}).get(line_key, 0)) >= int(qty_in_box)
            for line_key, qty_in_box in lines_map.items()
        )
    }

    # Обычные (не mixed) короба агрегируем по строкам как раньше.
    regular_grouped: dict[tuple[int, str, str, str, str, str, int], set[str]] = defaultdict(set)
    for box_id, lines_map in box_lines.items():
        if box_id not in fully_available_box_ids:
            continue
        if box_id in mixed_box_ids:
            continue
        for line_key, qty_in_box in lines_map.items():
            sku_id, sku_code, name, size, barcode, goods_type = line_key
            key = (sku_id, sku_code, name, size, barcode, goods_type, int(qty_in_box))
            regular_grouped[key].add(box_id)

    # Mixed-короба группируем по составу, чтобы можно было выбирать количество коробов
    # и автоматически добавлять все позиции из того же короба/типа короба.
    mixed_compositions: dict[
        tuple[tuple[int, str, str, str, str, str, int], ...],
        list[str],
    ] = defaultdict(list)
    for box_id in sorted(mixed_box_ids & fully_available_box_ids):
        composition: list[tuple[int, str, str, str, str, str, int]] = []
        for line_key, qty_in_box in box_lines.get(box_id, {}).items():
            sku_id, sku_code, name, size, barcode, goods_type = line_key
            composition.append(
                (
                    int(sku_id),
                    str(sku_code),
                    str(name),
                    str(size),
                    str(barcode),
                    str(goods_type),
                    int(qty_in_box),
                )
            )
        if not composition:
            continue
        mixed_compositions[tuple(sorted(composition))].append(box_id)
    prepared = []
    for (sku_id, sku_code, name, size, barcode, goods_type, box_qty), boxes in regular_grouped.items():
        boxes_count = len(boxes)
        if boxes_count <= 0:
            continue
        total_qty = boxes_count * int(box_qty)
        prepared.append(
            {
                "sku_code": sku_code,
                "sku_id": sku_id,
                "name": name,
                "size": size,
                "barcode": barcode,
                "goods_type": goods_type,
                "box_qty": int(box_qty),
                "boxes_count": boxes_count,
                "total_qty": total_qty,
                "box_codes": [box_codes_by_id.get(box_id, box_id.split(":", 1)[-1]) for box_id in sorted(boxes)],
                "is_mixed_box": False,
                "mixed_group": "",
            }
        )

    for composition_index, (composition, box_ids) in enumerate(sorted(mixed_compositions.items(), key=lambda item: item[1][0])):
        boxes_count = len(box_ids)
        if boxes_count <= 0:
            continue
        mixed_group = f"mix:{composition_index}:{box_ids[0]}"
        for sku_id, sku_code, name, size, barcode, goods_type, box_qty in composition:
            total_qty = boxes_count * int(box_qty)
            prepared.append(
                {
                    "sku_code": sku_code,
                    "sku_id": int(sku_id),
                    "name": name,
                    "size": size,
                    "barcode": barcode,
                    "goods_type": goods_type,
                    "box_qty": int(box_qty),
                    "boxes_count": boxes_count,
                    "total_qty": total_qty,
                    "box_codes": [box_codes_by_id.get(box_id, box_id.split(":", 1)[-1]) for box_id in sorted(box_ids)],
                    "is_mixed_box": True,
                    "mixed_group": mixed_group,
                }
            )

    prepared.sort(
        key=lambda row: (
            0 if row["is_mixed_box"] else 1,
            row["mixed_group"],
            -int(row["sku_id"] or 0),
            row["sku_code"].lower(),
            row["size"].lower(),
            StockAvailabilityService.normalize_goods_type(row["goods_type"]),
            -int(row["box_qty"] or 0),
        )
    )

    interim_rows: list[dict] = []
    for row in prepared:
        available_boxes = int(row["boxes_count"])
        interim_rows.append(
            {
                **row,
                "available_boxes": int(max(available_boxes, 0)),
                "available_qty": int(max(available_boxes, 0)) * int(row["box_qty"]),
            }
        )

    mixed_rows_by_group: dict[str, list[dict]] = defaultdict(list)
    for row in interim_rows:
        if row.get("is_mixed_box") and row.get("mixed_group"):
            mixed_rows_by_group[str(row["mixed_group"])].append(row)

    for group_rows in mixed_rows_by_group.values():
        group_available = min(int(item.get("available_boxes") or 0) for item in group_rows)
        for item in group_rows:
            item["available_boxes"] = max(group_available, 0)
            item["available_qty"] = max(group_available, 0) * int(item.get("box_qty") or 0)

    mixed_color_map: dict[str, int] = {}
    for index, group_key in enumerate(sorted(mixed_rows_by_group.keys())):
        mixed_color_map[group_key] = index % 6

    result: list[dict] = []
    for row in interim_rows:
        available_boxes = int(row.get("available_boxes") or 0)
        if available_boxes <= 0:
            continue
        available_qty = available_boxes * int(row["box_qty"])
        picker_key = _compose_picker_key(
            sku_id=row["sku_id"],
            sku_code=row["sku_code"],
            name=row["name"],
            size=row["size"],
            goods_type=row["goods_type"],
            box_qty=row["box_qty"],
            barcode=row["barcode"],
            mixed_group=row["mixed_group"],
        )
        result.append(
            {
                "key": picker_key,
                "sku_id": int(row["sku_id"] or 0),
                "sku_code": row["sku_code"],
                "name": row["name"],
                "size": row["size"],
                "barcode": row["barcode"],
                "goods_type": row["goods_type"],
                "box_qty": int(row["box_qty"]),
                "available_boxes": int(available_boxes),
                "available_qty": int(available_qty),
                "box_codes": list(row.get("box_codes") or []),
                "is_mixed_box": bool(row["is_mixed_box"]),
                "mixed_group": row["mixed_group"],
                "mixed_color": int(mixed_color_map.get(str(row["mixed_group"] or ""), -1)),
            }
        )
    return result


def _selected_stock_rows_with_boxes(
    request,
    stock_rows: list[dict],
    *,
    multiple: bool,
) -> tuple[list[tuple[dict, int]], int, list[str]]:
    stock_map = {row["key"]: row for row in stock_rows}
    mixed_group_rows: dict[str, list[dict]] = defaultdict(list)
    for row in stock_rows:
        if row.get("is_mixed_box") and row.get("mixed_group"):
            mixed_group_rows[str(row["mixed_group"])].append(row)
    errors: list[str] = []

    if multiple:
        keys = []
    else:
        keys = [request.POST.get("stock_key", "")]
    all_keys = request.POST.getlist("stock_key_all[]") or request.POST.getlist("stock_key_all")
    all_boxes = request.POST.getlist("stock_boxes[]") or request.POST.getlist("stock_boxes")
    boxes_by_key: dict[str, str] = {}
    for index, raw_key in enumerate(all_keys):
        key = str(raw_key or "").strip()
        if not key or key in boxes_by_key:
            continue
        boxes_by_key[key] = all_boxes[index] if index < len(all_boxes) else ""

    if multiple:
        positive_box_keys: list[str] = []
        seen_positive: set[str] = set()
        for key, raw_boxes in boxes_by_key.items():
            try:
                boxes = int(str(raw_boxes or "").strip())
            except ValueError:
                continue
            if boxes <= 0:
                continue
            if key not in seen_positive:
                seen_positive.add(key)
                positive_box_keys.append(key)
        keys = positive_box_keys

    explicit_rows: list[tuple[dict, int]] = []
    for raw_key in keys:
        key = str(raw_key or "").strip()
        if not key:
            continue
        row = stock_map.get(key)
        if row is None:
            errors.append("Выбрана неактуальная складская строка. Обновите страницу и повторите.")
            continue
        raw_boxes = boxes_by_key.get(key, "")
        try:
            boxes = int(str(raw_boxes or "").strip())
        except ValueError:
            errors.append(f"{row['sku_code']}: количество коробов должно быть целым числом.")
            continue
        if boxes <= 0:
            continue
        explicit_rows.append((row, boxes))

    mixed_group_boxes: dict[str, int] = {}
    for row, boxes in explicit_rows:
        group = str(row.get("mixed_group") or "").strip()
        if not (row.get("is_mixed_box") and group):
            continue
        previous = mixed_group_boxes.get(group)
        if previous is None:
            mixed_group_boxes[group] = boxes
            continue
        if previous != boxes:
            errors.append(
                "Для одного микс-короба укажите одинаковое количество коробов по выбранным строкам."
            )

    expanded_rows: list[tuple[dict, int]] = []
    expanded_groups: set[str] = set()
    counted_groups: set[str] = set()
    total_box_count = 0
    for row, boxes in explicit_rows:
        group = str(row.get("mixed_group") or "").strip()
        if row.get("is_mixed_box") and group:
            if group not in counted_groups:
                counted_groups.add(group)
                total_box_count += boxes
            if group in expanded_groups:
                continue
            expanded_groups.add(group)
            group_boxes = mixed_group_boxes.get(group, boxes)
            for sibling in mixed_group_rows.get(group, []):
                expanded_rows.append((sibling, group_boxes))
        else:
            total_box_count += boxes
            expanded_rows.append((row, boxes))

    return expanded_rows, int(max(total_box_count, 0)), errors


def _stock_row_split_payload(row: dict) -> dict:
    return {
        "row_key": str(row.get("key") or "").strip(),
        "sku_code": str(row.get("sku_code") or "").strip(),
        "name": str(row.get("name") or "").strip(),
        "size": str(row.get("size") or "").strip(),
        "barcode": str(row.get("barcode") or "").strip(),
        "goods_type": str(row.get("goods_type") or "").strip(),
        "qty": int(row.get("box_qty") or 0),
    }


def _selected_partial_box_items(
    request,
    stock_rows: list[dict],
    whole_box_counts_by_key: dict[str, int],
    whole_box_counts_by_group: dict[str, int],
) -> tuple[list[dict], int, list[str]]:
    stock_map = {str(row.get("key") or "").strip(): row for row in stock_rows if str(row.get("key") or "").strip()}
    mixed_group_rows: dict[str, list[dict]] = defaultdict(list)
    for row in stock_rows:
        group = str(row.get("mixed_group") or "").strip()
        if row.get("is_mixed_box") and group:
            mixed_group_rows[group].append(row)

    selected_items: list[dict] = []
    errors: list[str] = []
    total_split_boxes = 0
    split_boxes_by_key: dict[str, int] = defaultdict(int)
    split_boxes_by_group: dict[str, int] = defaultdict(int)

    for split_index, split in enumerate(_partial_box_splits_from_request(request), start=1):
        row_key = str(split.get("row_key") or "").strip()
        row = stock_map.get(row_key)
        if row is None:
            errors.append("Разбитый короб выбран из неактуальных остатков. Обновите страницу и повторите.")
            continue
        boxes = max(_to_int(split.get("boxes")), 0)
        if boxes <= 0:
            continue
        group = str(row.get("mixed_group") or "").strip()
        group_rows = list(mixed_group_rows.get(group) or []) if row.get("is_mixed_box") and group else [row]
        allowed_keys = {str(item.get("key") or "").strip() for item in group_rows}
        qty_by_key: dict[str, int] = {}
        for raw_item in split.get("items") or []:
            if not isinstance(raw_item, dict):
                continue
            item_key = str(raw_item.get("key") or "").strip()
            qty = max(_to_int(raw_item.get("qty")), 0)
            if not item_key or qty <= 0:
                continue
            if item_key not in allowed_keys:
                errors.append("В разбивке короба выбрана позиция из другого типа короба.")
                continue
            qty_by_key[item_key] = qty_by_key.get(item_key, 0) + qty
        if not qty_by_key:
            errors.append("Для разбитого короба укажите количество товара к отгрузке.")
            continue

        source_total_per_box = sum(max(int(item.get("box_qty") or 0), 0) for item in group_rows)
        pick_total_per_box = sum(qty_by_key.values())
        if pick_total_per_box >= source_total_per_box:
            errors.append("Если клиент забирает весь короб, используйте поле «К отгрузке (короба)».")
            continue

        row_by_key = {str(item.get("key") or "").strip(): item for item in group_rows}
        for item_key, pick_qty in qty_by_key.items():
            source_row = row_by_key.get(item_key)
            if source_row is None:
                continue
            source_qty = max(int(source_row.get("box_qty") or 0), 0)
            if source_qty <= 0 or pick_qty > source_qty:
                errors.append(
                    f"{source_row['sku_code']}/{source_row['size'] or '-'}: в коробе только {source_qty} шт."
                )
                continue

        source_box_pattern = [_stock_row_split_payload(source_row) for source_row in group_rows]
        pick_pattern = [
            {
                **_stock_row_split_payload(row_by_key[item_key]),
                "qty": int(qty),
            }
            for item_key, qty in qty_by_key.items()
            if item_key in row_by_key and int(qty or 0) > 0
        ]
        if not pick_pattern:
            continue

        group_key = str(split.get("group_key") or "").strip()
        if not group_key:
            group_key = f"partial:{group or row_key}:{split_index}"
        split_offset = int(split_boxes_by_group.get(group, 0)) if row.get("is_mixed_box") and group else int(split_boxes_by_key.get(row_key, 0))
        split_box_codes = [
            str(code or "").strip()
            for code in list(row.get("box_codes") or [])[split_offset:split_offset + boxes]
            if str(code or "").strip()
        ]
        if len(split_box_codes) < boxes:
            errors.append("Для разбитого короба не удалось определить исходные короба. Обновите страницу и повторите.")
            continue
        for item_key, pick_qty in qty_by_key.items():
            source_row = row_by_key.get(item_key)
            if source_row is None:
                continue
            meta = {
                "version": 1,
                "kind": "partial_box_split",
                "group_key": group_key,
                "row_key": item_key,
                "source_group": group,
                "source_boxes": int(boxes),
                "source_box_total_qty": int(source_total_per_box),
                "source_box_pattern": source_box_pattern,
                "source_box_codes": split_box_codes,
                "pick_pattern": pick_pattern,
                "item_pick_qty": int(pick_qty),
                "item_source_qty": int(source_row.get("box_qty") or 0),
                "is_mixed_box": bool(source_row.get("is_mixed_box")),
            }
            encoded_meta = encode_partial_box_split(meta)
            selected_items.append(
                {
                    "sku_code": str(source_row.get("sku_code") or "").strip(),
                    "name": str(source_row.get("name") or "").strip(),
                    "size": str(source_row.get("size") or "").strip(),
                    "barcode": str(source_row.get("barcode") or "").strip(),
                    "goods_type": str(source_row.get("goods_type") or "").strip(),
                    "qty_requested": int(boxes) * int(pick_qty),
                    "comment": (
                        f"Исходные короба: {', '.join(split_box_codes)}; "
                        f"Разбить короб: {boxes}; отбор из короба: {pick_qty} "
                        f"из {int(source_row.get('box_qty') or 0)}; {encoded_meta}"
                    ),
                }
            )

        if row.get("is_mixed_box") and group:
            split_boxes_by_group[group] += boxes
        else:
            split_boxes_by_key[row_key] += boxes
        total_split_boxes += boxes

    for key, split_boxes in split_boxes_by_key.items():
        row = stock_map.get(key)
        if row is None:
            continue
        selected_total = int(whole_box_counts_by_key.get(key, 0)) + int(split_boxes)
        if selected_total > int(row.get("available_boxes") or 0):
            errors.append(
                f"{row['sku_code']}/{row['size'] or '-'}: выбрано {selected_total} короб., доступно {row['available_boxes']}."
            )
    for group, split_boxes in split_boxes_by_group.items():
        rows = mixed_group_rows.get(group) or []
        if not rows:
            continue
        available = min(int(row.get("available_boxes") or 0) for row in rows)
        selected_total = int(whole_box_counts_by_group.get(group, 0)) + int(split_boxes)
        if selected_total > available:
            errors.append(f"Для микс-короба выбрано {selected_total} короб., доступно {available}.")

    return selected_items, int(max(total_split_boxes, 0)), errors


def _parse_selected_stock_items(
    request,
    stock_rows: list[dict],
    *,
    multiple: bool,
) -> tuple[list[dict], list[str]]:
    expanded_rows, _total_box_count, errors = _selected_stock_rows_with_boxes(
        request,
        stock_rows,
        multiple=multiple,
    )
    selected_map: dict[tuple[str, str, str, str, str, str, str], dict] = {}
    whole_box_counts_by_key: dict[str, int] = defaultdict(int)
    whole_box_counts_by_group: dict[str, int] = {}
    split_box_counts_by_key, split_box_counts_by_group = _partial_box_counts_from_request(request, stock_rows)

    for row, boxes in expanded_rows:
        if boxes > int(row["available_boxes"]):
            errors.append(
                f"{row['sku_code']}/{row['size'] or '-'}: доступно только {row['available_boxes']} короб."
            )
            continue
        group = str(row.get("mixed_group") or "").strip()
        if row.get("is_mixed_box") and group:
            whole_box_counts_by_group[group] = max(int(whole_box_counts_by_group.get(group) or 0), int(boxes))
        else:
            whole_box_counts_by_key[str(row.get("key") or "").strip()] += int(boxes)
        qty_requested = boxes * int(row["box_qty"])
        selected_code_offset = int(split_box_counts_by_group.get(group, 0)) if row.get("is_mixed_box") and group else int(split_box_counts_by_key.get(str(row.get("key") or "").strip(), 0))
        selected_box_codes = [
            str(code or "").strip()
            for code in list(row.get("box_codes") or [])[selected_code_offset:selected_code_offset + boxes]
            if str(code or "").strip()
        ]
        if row.get("box_codes") and len(selected_box_codes) < boxes:
            errors.append(
                f"{row['sku_code']}/{row['size'] or '-'}: не удалось определить выбранные короба."
            )
            continue
        item_key = (
            str(row.get("sku_code") or "").strip(),
            str(row.get("name") or "").strip(),
            str(row.get("size") or "").strip(),
            str(row.get("barcode") or "").strip(),
            str(row.get("goods_type") or "").strip(),
            str(int(row.get("box_qty") or 0)),
            ",".join(selected_box_codes),
        )
        existing = selected_map.get(item_key)
        comment = f"Коробов: {boxes}; кратность: {row['box_qty']}"
        if selected_box_codes:
            comment += f"; короба: {', '.join(selected_box_codes)}"
        if row.get("is_mixed_box"):
            comment += "; микс-короб"
        if existing:
            existing["qty_requested"] += qty_requested
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
    partial_items, _split_box_count, partial_errors = _selected_partial_box_items(
        request,
        stock_rows,
        whole_box_counts_by_key,
        whole_box_counts_by_group,
    )
    errors.extend(partial_errors)
    for item in partial_items:
        item_key = (
            str(item.get("sku_code") or "").strip(),
            str(item.get("name") or "").strip(),
            str(item.get("size") or "").strip(),
            str(item.get("barcode") or "").strip(),
            str(item.get("goods_type") or "").strip(),
            str(item.get("comment") or "").strip(),
            "partial_box_split",
        )
        selected_map[item_key] = item
    selected = list(selected_map.values())
    if not selected:
        errors.append("Выберите минимум одну позицию из складских остатков: целый короб или разбивку короба.")
    return selected, errors


@login_required
def shipping_list(request):
    scope, role, client_agency = _request_scope(request)
    if scope is None:
        return HttpResponseForbidden("Доступ запрещен")
    context = build_shipping_list_page_context(
        request=request,
        scope=scope,
        role=role,
        client_agency=client_agency,
    )
    return render(request, "shipping/list.html", context)


@login_required
def shipping_create(request):
    scope, role, client_agency = _request_scope(request)
    if scope is None or not _can_write(scope, role):
        return HttpResponseForbidden("Доступ запрещен")
    return handle_shipping_create_request(
        request=request,
        scope=scope,
        role=role,
        client_agency=client_agency,
    )


@login_required
def shipping_detail(request, pk: int):
    scope, role, client_agency = _request_scope(request)
    if scope is None:
        return HttpResponseForbidden("Доступ запрещен")

    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "created_by", "marketplace").prefetch_related("items", "reserves"),
        pk=pk,
    )
    if not _can_access_order(scope, client_agency, order):
        return HttpResponseForbidden("Доступ запрещен")

    can_write = _can_write(scope, role)
    can_edit_items = _can_edit_items(scope, role, order)
    can_submit_for_approval = _can_submit_for_approval(scope, role, order)
    can_manager_approve = _can_manager_approve(scope, role, order)
    can_manager_reopen = _can_manager_reopen(scope, role, order)
    can_storekeeper_accept = _can_storekeeper_accept(scope, role, order)
    can_storekeeper_pick = _can_storekeeper_pick(scope, role, order)
    pick_readiness = shipping_pick_readiness(order) if can_storekeeper_pick else {"can_pick": False, "reason": ""}
    can_storekeeper_pack = _can_storekeeper_pack(scope, role, order)
    can_storekeeper_manage_packing = _can_storekeeper_manage_packing(scope, role, order)
    can_cancel = _can_cancel(scope, role, order)
    can_edit_order_form = _can_edit_order_form(scope, role, order)
    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("Доступ запрещен")
        stock_rows_for_order = _shipping_stock_picker_rows(order.agency, exclude_order=order)
        action_result = handle_shipping_detail_action(
            action=(request.POST.get("action") or "").strip(),
            order=order,
            request=request,
            role=role,
            stock_rows_for_order=stock_rows_for_order,
            permissions=ShippingDetailActionPermissions(
                can_edit_items=can_edit_items,
                can_submit_for_approval=can_submit_for_approval,
                can_manager_approve=can_manager_approve,
                can_manager_reopen=can_manager_reopen,
                can_storekeeper_accept=can_storekeeper_accept,
                can_storekeeper_pick=can_storekeeper_pick,
                can_cancel=can_cancel,
            ),
            parse_selected_stock_items=_parse_selected_stock_items,
            selected_stock_rows_with_boxes=_selected_stock_rows_with_boxes,
            parse_box_count_from_comment=_parse_box_count_from_comment,
            log_update=_log_update,
        )
        for level, text in action_result.messages:
            getattr(messages, level)(request, text)
        if action_result.redirect_url:
            return redirect(action_result.redirect_url)
        if action_result.redirect_name == "shipping:packing":
            return redirect("shipping:packing", pk=order.pk)
        return redirect("shipping:detail", pk=order.pk)
    context = build_shipping_detail_page_context(
        request=request,
        order=order,
        scope=scope,
        role=role,
    )
    return render(request, "shipping/detail.html", context)


@login_required
def shipping_dispatch_act(request, pk: int):
    return handle_shipping_dispatch_act_request(request=request, pk=pk)


@login_required
def shipping_sign_dispatch_act_logistician(request, pk: int):
    return handle_shipping_dispatch_sign_logistician_request(request=request, pk=pk)


@login_required
def shipping_sign_dispatch_act_manager(request, pk: int):
    return handle_shipping_dispatch_sign_manager_request(request=request, pk=pk)


@login_required
def shipping_attachment_download(request, pk: int, attachment_id: int):
    return download_shipping_attachment(request=request, pk=pk, attachment_id=attachment_id)


def _shipping_documents_response(request, pk: int):
    return handle_shipping_documents_request(request=request, pk=pk)


@login_required
def shipping_documents(request, pk: int):
    return _shipping_documents_response(request, pk)


@login_required
def shipping_transport_note(request, pk: int):
    return _shipping_documents_response(request, pk)


@login_required
@xframe_options_sameorigin
def shipping_transport_note_pdf(request, pk: int):
    return build_shipping_transport_note_pdf_response(request=request, pk=pk)


@login_required
def shipping_transport_note_docx(request, pk: int):
    return build_shipping_transport_note_docx_response(request=request, pk=pk)


@login_required
def shipping_return_act_doc(request, pk: int):
    return build_shipping_return_act_response(request=request, pk=pk)


@login_required
def shipping_packing(request, pk: int):
    return handle_shipping_packing_request(request=request, pk=pk)


@login_required
@require_http_methods(["POST"])
def shipping_packing_pallet_removal_request(request, pk: int):
    return handle_shipping_packing_pallet_removal_request(request=request, pk=pk)


@login_required
@require_http_methods(["GET", "POST"])
def shipping_packing_pallet_removal_review(request, pk: int):
    return handle_shipping_packing_pallet_removal_review_request(request=request, pk=pk)


@login_required
def shipping_loose_packing(request, pk: int):
    return handle_shipping_loose_packing_request(request=request, pk=pk)


@login_required
@require_http_methods(["POST"])
def shipping_loose_packing_container_code(request, pk: int):
    scope, role, client_agency = _request_scope(request)
    if scope != "staff" or not _is_storekeeper_role(role):
        return HttpResponseForbidden("Р”РѕСЃС‚СѓРї Р·Р°РїСЂРµС‰РµРЅ")
    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency"),
        pk=pk,
    )
    if not _can_access_order(scope, client_agency, order):
        return HttpResponseForbidden("Р”РѕСЃС‚СѓРї Р·Р°РїСЂРµС‰РµРЅ")
    if order.is_closed() or order.status not in {ShippingOrder.STATUS_PICKING, ShippingOrder.STATUS_PACKED}:
        return JsonResponse({"ok": False, "error": "not_allowed"}, status=403)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = request.POST.dict()
    if not isinstance(payload, dict):
        payload = {}
    goods_type = str(payload.get("goods_type") or "").strip()
    container_kind = normalize_container_kind(payload.get("kind"))
    if container_kind == "pallet":
        issued = issue_pallet_code(
            agency=order.agency,
            goods_type=goods_type,
            order_type="shipping",
            order_id=order.number,
        )
    else:
        issued = issue_container_code(
            agency=order.agency,
            goods_type=goods_type,
        )
    return JsonResponse({"ok": True, **issued})


@login_required
def shipping_packing_slips(request, pk: int):
    return handle_shipping_packing_slips_request(request=request, pk=pk)


@login_required
@require_http_methods(["GET", "POST"])
def shipping_packing_slips_status(request, pk: int):
    return handle_shipping_packing_slips_status_request(request=request, pk=pk)
