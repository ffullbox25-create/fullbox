"""Stock map UI helpers and class-based views."""

import json
import re
from collections import Counter

from django.db.models import Prefetch, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin, get_request_role, resolve_cabinet_url
from employees.models import Employee
from reachtruck.models import MoveTask
from sku.models import Agency
from sklad.models import WarehouseContainer, WarehouseOperation, WarehouseStockSnapshot
from sklad.location_occupancy import FBS_STORAGE_CONTEXT_TYPE
from sklad.services.putaway_draft_reservations import PutawayDraftReservationService
from sklad.services.stock_availability import StockAvailabilityService
from sklad.topology import (
    OS_CELLS_PER_TIER,
    OS_LINE_DISPLAY_LABELS,
    OS_PASSAGE_POSITIONS,
    OS_ROW_SECTIONS,
    OS_TIERS,
    OS_TIER_OVERRIDES,
    iter_os_slot_keys,
)
from .services import (
    build_stock_map_context,
    build_stock_map_pr_context,
    build_stock_map_row_context,
    build_stock_map_visual_context,
    parse_pr_destinations_json,
    reconcile_picker_draft_payload,
    submit_stock_map_pr_moves,
)

_OS_ROW_SECTIONS = OS_ROW_SECTIONS
_OS_TIERS = OS_TIERS
_OS_CELLS_PER_TIER = OS_CELLS_PER_TIER
_OS_TIER_OVERRIDES = OS_TIER_OVERRIDES
_OS_PASSAGE_POSITIONS = OS_PASSAGE_POSITIONS

_OS_ROW_BADGE_BG_START = (226, 222, 216)
_OS_ROW_BADGE_BG_END = (120, 22, 22)
_OS_ROW_BADGE_BORDER_START = (197, 191, 184)
_OS_ROW_BADGE_BORDER_END = (92, 14, 14)
_OS_ROW_BADGE_INK_START = (104, 99, 92)
_OS_ROW_BADGE_INK_END = (255, 241, 241)
_OS_ROW_BADGE_SHADOW_RGB = (120, 22, 22)
_PALLET_CONTAINER_TYPES = {
    WarehouseContainer.TYPE_PALLET,
    WarehouseContainer.TYPE_MIXED_PALLET,
}
_OS_LINE_DISPLAY_LABELS = OS_LINE_DISPLAY_LABELS


def _os_tiers_for_position(row_number: int, section_number: int) -> int:
    row_no = _int_value(row_number)
    section_no = _int_value(section_number)
    if row_no <= 0 or section_no <= 0:
        return 0
    if section_no > _int_value(_OS_ROW_SECTIONS.get(row_no)):
        return 0
    return _int_value(_OS_TIER_OVERRIDES.get((row_no, section_no))) or _OS_TIERS


def _os_line_display_label(section_number: int) -> str:
    return _OS_LINE_DISPLAY_LABELS.get(_int_value(section_number), str(_int_value(section_number) or ""))


def _os_location_code(*, row: int, section: int, tier: int = 0, cell: int = 0) -> str:
    line_label = _os_line_display_label(section)
    rack_no = _int_value(row)
    tier_no = _int_value(tier)
    cell_no = _int_value(cell)
    if line_label and rack_no and tier_no and cell_no:
        return f"{line_label}-{rack_no}/{tier_no}-{cell_no}"
    if line_label and rack_no:
        return f"{line_label}-{rack_no}"
    return line_label or (str(rack_no) if rack_no else "")


def _os_max_tiers() -> int:
    return max([_OS_TIERS, *(_int_value(value) for value in _OS_TIER_OVERRIDES.values())])


def _os_max_tiers_for_row(row_number: int) -> int:
    sections_total = _int_value(_OS_ROW_SECTIONS.get(_int_value(row_number)))
    if sections_total <= 0:
        return 0
    return max(_os_tiers_for_position(row_number, section_number) for section_number in range(1, sections_total + 1))


def _os_max_tiers_for_section(section_number: int) -> int:
    section_no = _int_value(section_number)
    if section_no <= 0:
        return 0
    tiers = [
        _os_tiers_for_position(row_number, section_no)
        for row_number in sorted(_OS_ROW_SECTIONS.keys())
        if section_no <= _int_value(_OS_ROW_SECTIONS.get(row_number))
    ]
    return max(tiers, default=0)


def _os_is_passage_position(row_number: int, section_number: int, tier_number: int) -> bool:
    return (
        _int_value(row_number),
        _int_value(section_number),
        _int_value(tier_number),
    ) in _OS_PASSAGE_POSITIONS


def _os_total_slots_for_row(row_number: int) -> int:
    row_no = _int_value(row_number)
    sections_total = _int_value(_OS_ROW_SECTIONS.get(row_no))
    if sections_total <= 0:
        return 0
    total = 0
    for section_number in range(1, sections_total + 1):
        for tier_number in range(1, _os_tiers_for_position(row_no, section_number) + 1):
            if _os_is_passage_position(row_no, section_number, tier_number):
                continue
            total += _OS_CELLS_PER_TIER
    return total


class StockMapView(RoleRequiredMixin, TemplateView):
    template_name = "stockmap/stockmap.html"
    allowed_roles = ("storekeeper", "processing_head", "head_manager", "director", "admin")

    def get(self, request, *args, **kwargs):
        query = request.GET.urlencode()
        target = "/stockmap/visual/"
        if query:
            target = f"{target}?{query}"
        return redirect(target)


class StockMapRowView(RoleRequiredMixin, TemplateView):
    template_name = "stockmap/stockmap_row.html"
    allowed_roles = ("storekeeper", "processing_head", "head_manager", "director", "admin")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        row_number = _int_value(kwargs.get("row"))
        context.update(build_stock_map_row_context(request=self.request, row_number=row_number))
        return context


class StockMapVisualView(RoleRequiredMixin, TemplateView):
    template_name = "stockmap/stockmap_visual.html"
    allowed_roles = ("storekeeper", "processing_head", "head_manager", "director", "admin")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(build_stock_map_visual_context(request=self.request))
        return context


class StockMapPrView(RoleRequiredMixin, TemplateView):
    template_name = "stockmap/stockmap_pr.html"
    allowed_roles = ("storekeeper", "processing_head", "head_manager", "director", "admin")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(build_stock_map_pr_context(request=self.request))
        return context

    def post(self, request, *args, **kwargs):
        return submit_stock_map_pr_moves(request=request)

    @staticmethod
    def _parse_destinations_json(raw: str) -> dict[str, dict]:
        return parse_pr_destinations_json(raw)


class StockMapPickerDraftView(RoleRequiredMixin, View):
    allowed_roles = ("storekeeper", "processing_head", "head_manager", "director", "admin")

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads((request.body or b"").decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"ok": False, "error": "Некорректный JSON."}, status=400)
        if not isinstance(payload, dict):
            return JsonResponse({"ok": False, "error": "Некорректные данные бронирования."}, status=400)

        action = _clean_text(payload.get("action")).lower()
        draft_token = _clean_text(payload.get("draft_token"))
        if action == "heartbeat":
            touched = PutawayDraftReservationService.touch(draft_token=draft_token)
            return JsonResponse(
                {
                    "ok": True,
                    "touched": int(touched or 0),
                    "expires_in_seconds": int(PutawayDraftReservationService.SESSION_TIMEOUT.total_seconds()),
                }
            )
        if action == "release":
            released = PutawayDraftReservationService.release(draft_token=draft_token)
            return JsonResponse({"ok": True, "released_count": int(released or 0)})
        if action != "sync":
            return JsonResponse({"ok": False, "error": "Неизвестное действие."}, status=400)

        order_id = _clean_text(payload.get("order_id"))
        agency = Agency.objects.filter(id=_int_value(payload.get("agency_id"))).first()
        if agency is None and order_id:
            latest_snapshot = (
                WarehouseStockSnapshot.objects.select_related("agency")
                .filter(source_context_type="receiving", source_context_id=order_id, is_archived=False)
                .order_by("-id")
                .first()
            )
            agency = latest_snapshot.agency if latest_snapshot else None
        result = PutawayDraftReservationService.sync_receiving_putaway_draft(
            draft_token=draft_token,
            order_id=order_id,
            agency=agency,
            rows=payload.get("rows") if isinstance(payload.get("rows"), list) else [],
            requested_by=request.user if request.user.is_authenticated else None,
            requested_by_role=get_request_role(request) or "",
        )
        hydrated_state = reconcile_picker_draft_payload(
            {
                "order_id": order_id,
                "agency_id": int(agency.id) if agency else _int_value(payload.get("agency_id")),
                "client_marker": _clean_text(payload.get("client_marker")),
                "active_pallet_code": _clean_text(payload.get("active_pallet_code") or payload.get("active_pallet")),
                "rows": payload.get("rows") if isinstance(payload.get("rows"), list) else [],
            },
            draft_token=draft_token,
        )
        response_status = 200 if result.ok else 409
        return JsonResponse(
            {
                "ok": result.ok,
                "error": result.error_message,
                "released_count": int(result.released_count or 0),
                "reserved_count": len(result.reserved_rows),
                "reserved_rows": [
                    {
                        "pallet_code": _clean_text(row.get("pallet_code")),
                        "destination": row.get("destination") if isinstance(row.get("destination"), dict) else {},
                    }
                    for row in result.reserved_rows
                    if isinstance(row, dict)
                ],
                "conflict_rows": [
                    {
                        "pallet_code": _clean_text(row.get("pallet_code")),
                        "destination": row.get("destination") if isinstance(row.get("destination"), dict) else {},
                        "reason": _clean_text(row.get("reason")),
                    }
                    for row in result.conflict_rows
                    if isinstance(row, dict)
                ],
                "hydrated_rows": hydrated_state.get("rows") if isinstance(hydrated_state.get("rows"), list) else [],
                "expires_in_seconds": int(result.expires_in_seconds or 0),
            },
            status=response_status,
        )


def _int_value(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_zone(value: str) -> str:
    text = (value or "").strip().upper()
    if not text:
        return ""
    if text in {"PR", "OBR", "OTG", "MR", "OS"}:
        return text
    if "ПРИЕМ" in text:
        return "PR"
    if "ОБРАБОТ" in text:
        return "OBR"
    if "ОТГРУЗ" in text:
        return "OTG"
    if "МЕЖДУ" in text:
        return "MR"
    if "ОСНОВ" in text:
        return "OS"
    return text


def _warehouse_pallet_locations(*, stock_rows: list[dict] | None = None) -> list[dict]:
    rows = stock_rows if stock_rows is not None else _active_stockmap_rows(rebuild_if_empty=True)
    grouped: dict[tuple[int, str, str, str], Counter] = {}
    for row in rows:
        key = (
            int(row.get("agency_id") or 0),
            str(row.get("order_type") or "").strip(),
            str(row.get("order_id") or "").strip(),
            str(row.get("pallet_code") or "").strip(),
        )
        if not key[3]:
            continue
        zone = _normalize_zone(str(row.get("zone") or ""))
        cell_key = (
            zone,
            _int_value(row.get("row")),
            _int_value(row.get("section")),
            _int_value(row.get("tier")),
            _int_value(row.get("cell")),
        )
        grouped.setdefault(key, Counter())[cell_key] += 1

    result = []
    for (_, _, _, pallet_code), counter in grouped.items():
        (zone, row, section, tier, cell), _ = counter.most_common(1)[0]
        result.append(
            {
                "pallet_code": pallet_code,
                "zone": zone,
                "row": row,
                "section": section,
                "tier": tier,
                "cell": cell,
            }
        )
    return result


def _os_cell_key(section: int, tier: int, cell: int) -> str:
    return f"{int(section or 0)}:{int(tier or 0)}:{int(cell or 0)}"


def _clean_text(value) -> str:
    return str(value or "").strip()


_GENERIC_DISPLAY_MARKERS = {"PAL", "PALLET", "BOX", "SKU", "MIX", "PLT"}


def _stock_display_marker_token(value) -> str:
    text = _clean_text(value).upper()
    if not text:
        return ""
    token = re.split(r"[\s/_-]+", text, maxsplit=1)[0]
    token = "".join(char for char in token if char.isalnum())
    return token[:4]


def _stock_display_marker(*, pallet_code: str = "", sku_code: str = "") -> str:
    pallet_marker = _stock_display_marker_token(pallet_code)
    sku_marker = _stock_display_marker_token(sku_code)
    for candidate in (pallet_marker, sku_marker):
        if candidate and candidate not in _GENERIC_DISPLAY_MARKERS:
            return candidate
    return pallet_marker or sku_marker or ""


def _client_index_marker(
    agency_id: int | str | None,
    *,
    agency: Agency | None = None,
    pallet_code: str = "",
    sku_code: str = "",
) -> str:
    prefix = _stock_display_marker_token(getattr(agency, "pref", ""))
    if prefix:
        return prefix
    fallback_marker = _stock_display_marker(pallet_code=pallet_code, sku_code=sku_code)
    if fallback_marker:
        return fallback_marker
    value = _int_value(agency_id)
    return str(value) if value > 0 else ""


def _reservation_stage_label(kind: str) -> str:
    token = _clean_text(kind).lower()
    if token == "draft":
        return "Черновая бронь"
    if token == "planned":
        return "Передано ричтраку"
    return "Размещено"


def _active_os_reservation_rows(*, row_number: int | None = None, current_draft_token: str = "") -> list[dict]:
    PutawayDraftReservationService.cleanup_expired()
    qs = (
        WarehouseOperation.objects.select_related("agency", "destination_location")
        .prefetch_related("tasks__container")
        .filter(
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            status__in=[
                WarehouseOperation.STATUS_CREATED,
                WarehouseOperation.STATUS_PLANNED,
                WarehouseOperation.STATUS_IN_PROGRESS,
                WarehouseOperation.STATUS_PARTIAL,
                WarehouseOperation.STATUS_BLOCKED,
            ],
            destination_location__zone_code__iexact="OS",
        )
        .order_by("id")
    )
    if row_number:
        qs = qs.filter(destination_location__row_no=row_number)
    normalized_draft_token = _clean_text(current_draft_token)
    rows: list[dict] = []
    for operation in qs:
        location = operation.destination_location
        if location is None:
            continue
        reservation_kind = (
            "draft"
            if PutawayDraftReservationService.is_draft_operation(operation)
            else "planned"
        )
        tasks = list(operation.tasks.all())
        first_task = tasks[0] if tasks else None
        task_payload = dict(getattr(first_task, "payload", {}) or {})
        pallet_code = (
            _clean_text(operation.comment)
            if reservation_kind == "draft"
            else (
                _clean_text(getattr(getattr(first_task, "container", None), "container_code", ""))
                or _clean_text(task_payload.get("pallet_code"))
                or _clean_text(task_payload.get("container_code"))
            )
        )
        row = _int_value(location.row_no)
        section = _int_value(location.section_no)
        tier = _int_value(location.tier_no)
        cell = _int_value(location.cell_no)
        rows.append(
            {
                "agency": operation.agency,
                "agency_id": int(operation.agency_id or 0),
                "client_name": (
                    _clean_text(getattr(operation.agency, "short_name", ""))
                    or _clean_text(getattr(operation.agency, "agn_name", ""))
                    or f"Клиент {operation.agency_id}"
                ),
                "client_marker": _client_index_marker(
                    operation.agency_id,
                    agency=operation.agency,
                    pallet_code=pallet_code,
                ),
                "order_type": _clean_text(operation.source_document_type) or _clean_text(operation.context_type),
                "order_id": _clean_text(operation.source_document_id) or _clean_text(operation.context_id),
                "pallet_code": pallet_code or "Бронь",
                "zone": "OS",
                "row": row,
                "section": section,
                "tier": tier,
                "cell": cell,
                "location": _clean_text(location.display_name)
                or _location_label({"zone": "OS", "row": row, "section": section, "tier": tier, "cell": cell}),
                "reservation_kind": reservation_kind,
                "stage_label": _reservation_stage_label(reservation_kind),
                "status": _clean_text(operation.status).lower(),
                "is_owned_by_current_draft": (
                    reservation_kind == "draft"
                    and normalized_draft_token
                    and _clean_text(operation.context_id) == normalized_draft_token
                ),
            }
        )
    rows.extend(_active_fbs_os_rows(row_number=row_number))
    return rows


def _active_fbs_os_rows(*, row_number: int | None = None) -> list[dict]:
    from fbs.models import FbsBox, FbsPallet

    active_box_statuses = (FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE)
    boxes = FbsBox.objects.filter(status__in=active_box_statuses).order_by("box_code", "id")
    pallets = (
        FbsPallet.objects.select_related(
            "agency",
            "cell__location",
            "warehouse_container",
        )
        .prefetch_related(Prefetch("boxes", queryset=boxes, to_attr="active_storage_boxes"))
        .filter(
            status__in=(FbsPallet.STATUS_PLANNED, FbsPallet.STATUS_ACTIVE),
            cell__is_active=True,
            cell__location__zone_code__iexact="OS",
            warehouse_container__status=WarehouseContainer.STATUS_ACTIVE,
            warehouse_container__source_context_type=FBS_STORAGE_CONTEXT_TYPE,
        )
        .order_by("cell__location__row_no", "cell__location__section_no", "id")
    )
    if row_number:
        pallets = pallets.filter(cell__location__row_no=row_number)

    rows: list[dict] = []
    for pallet in pallets:
        location = pallet.cell.location
        box_codes = [
            _clean_text(box.box_code)
            for box in pallet.active_storage_boxes
            if _clean_text(box.box_code)
        ]
        is_planned = pallet.status == FbsPallet.STATUS_PLANNED
        rows.append(
            {
                "agency": pallet.agency,
                "agency_id": int(pallet.agency_id or 0),
                "client_name": (
                    _clean_text(getattr(pallet.agency, "short_name", ""))
                    or _clean_text(getattr(pallet.agency, "agn_name", ""))
                    or f"Клиент {pallet.agency_id}"
                ),
                "client_marker": _client_index_marker(
                    pallet.agency_id,
                    agency=pallet.agency,
                    pallet_code=pallet.pallet_code,
                ),
                "order_type": "FBS",
                "order_id": "хранение",
                "pallet_code": _clean_text(pallet.pallet_code) or "FBS-паллета",
                "zone": "OS",
                "row": _int_value(location.row_no),
                "section": _int_value(location.section_no),
                "tier": _int_value(location.tier_no),
                "cell": _int_value(location.cell_no),
                "location": _clean_text(location.display_name)
                or _location_label(
                    {
                        "zone": "OS",
                        "row": location.row_no,
                        "section": location.section_no,
                        "tier": location.tier_no,
                        "cell": location.cell_no,
                    }
                ),
                "reservation_kind": "planned" if is_planned else "placed",
                "stage_label": (
                    "FBS · место зарезервировано"
                    if is_planned
                    else "FBS · размещено"
                ),
                "status": _clean_text(pallet.status).lower(),
                "box_codes": box_codes,
                "row_count": 0,
                "is_owned_by_current_draft": False,
            }
        )
    return rows


def _unlocated_active_fbs_pallet_rows() -> list[dict]:
    """Active FBS stock that cannot be resolved to a valid physical OS slot."""
    from fbs.models import FbsPallet

    valid_slots = set(iter_os_slot_keys())
    pallets = (
        FbsPallet.objects.filter(status=FbsPallet.STATUS_ACTIVE)
        .select_related("agency", "cell__location")
        .annotate(stock_qty=Sum("boxes__stock_balances__qty"))
        .order_by("pallet_code", "id")
    )
    result: list[dict] = []
    for pallet in pallets:
        location = pallet.cell.location
        location_key = (
            _int_value(location.row_no),
            _int_value(location.section_no),
            _int_value(location.tier_no),
            _int_value(location.cell_no),
        )
        if _normalize_zone(location.zone_code or "") == "OS" and location_key in valid_slots:
            continue
        result.append(
            {
                "pallet_code": _clean_text(pallet.pallet_code) or f"FBS-паллета #{pallet.id}",
                "client_name": (
                    _clean_text(getattr(pallet.agency, "short_name", ""))
                    or _clean_text(getattr(pallet.agency, "agn_name", ""))
                    or f"Клиент {pallet.agency_id}"
                ),
                "cell_code": _clean_text(pallet.cell.cell_code) or "Без адреса",
                "qty": int(pallet.stock_qty or 0),
            }
        )
    return result


def _location_parts_from_snapshot(snapshot: WarehouseStockSnapshot) -> tuple[str, int, int, int, int, str]:
    location = snapshot.location
    if location is not None:
        zone = _normalize_zone(location.zone_code or snapshot.zone_code or "")
        row = _int_value(location.row_no)
        section = _int_value(location.section_no)
        tier = _int_value(location.tier_no)
        cell = _int_value(location.cell_no)
        location_label = (
            _clean_text(location.display_name)
            or _clean_text(location.location_code)
            or _location_label(
                {
                    "zone": zone,
                    "row": row,
                    "section": section,
                    "tier": tier,
                    "cell": cell,
                }
            )
        )
        return zone, row, section, tier, cell, location_label
    zone = _normalize_zone(snapshot.zone_code or "")
    return zone, 0, 0, 0, 0, _location_label({"zone": zone})


def _stockmap_row_from_snapshot(snapshot: WarehouseStockSnapshot) -> dict | None:
    container = snapshot.container
    parent_container = snapshot.parent_container
    pallet_code = ""
    box_code = ""
    if parent_container is not None and _clean_text(parent_container.container_code):
        pallet_code = _clean_text(parent_container.container_code)
        if container is not None and _clean_text(container.container_code):
            box_code = _clean_text(container.container_code)
    elif container is not None and container.container_type in _PALLET_CONTAINER_TYPES:
        pallet_code = _clean_text(container.container_code)
    elif _clean_text(snapshot.container_code):
        pallet_code = _clean_text(snapshot.container_code)
    if not pallet_code:
        return None
    zone, row, section, tier, cell, location_label = _location_parts_from_snapshot(snapshot)
    return {
        "agency": snapshot.agency,
        "agency_id": int(snapshot.agency_id or 0),
        "order_type": _clean_text(snapshot.source_context_type),
        "order_id": _clean_text(snapshot.source_context_id),
        "sku": _clean_text(snapshot.sku_code),
        "name": _clean_text(snapshot.name),
        "size": _clean_text(snapshot.size),
        "barcode": _clean_text(snapshot.barcode),
        "goods_type": _clean_text(snapshot.goods_type),
        "qty": int(snapshot.qty or 0),
        "available_qty": int(snapshot.available_qty or 0),
        "processing_reserved_qty": int(snapshot.processing_reserved_qty or 0),
        "shipping_reserved_qty": int(snapshot.shipping_reserved_qty or 0),
        "pallet_code": pallet_code,
        "box_code": box_code,
        "zone": zone,
        "row": row,
        "section": section,
        "tier": tier,
        "cell": cell,
        "location": location_label,
    }


def _warehouse_snapshot_rows(*, zone: str | None = None, row_number: int | None = None) -> list[dict]:
    qs = (
        WarehouseStockSnapshot.objects.filter(is_archived=False)
        .exclude(container_code__isnull=True)
        .exclude(container_code="")
        .select_related("agency", "container", "parent_container", "location")
        .order_by("updated_at", "id")
    )
    normalized_zone = _normalize_zone(zone or "")
    if normalized_zone:
        qs = qs.filter(zone_code__iexact=normalized_zone)
    if row_number:
        qs = qs.filter(location__row_no=row_number)
    rows = []
    for snapshot in qs:
        row = _stockmap_row_from_snapshot(snapshot)
        if row is None:
            continue
        if normalized_zone and row["zone"] != normalized_zone:
            continue
        if row_number and row["row"] != row_number:
            continue
        rows.append(row)
    return rows


def _active_stockmap_rows(*, zone: str | None = None, row_number: int | None = None, rebuild_if_empty: bool = False) -> list[dict]:
    snapshot_rows = _warehouse_snapshot_rows(zone=zone, row_number=row_number)
    return snapshot_rows


def _resolved_box_count(box_codes: set[str], row_count: int) -> int:
    explicit_count = len([code for code in box_codes if code])
    if explicit_count:
        return explicit_count
    return int(row_count or 0) if int(row_count or 0) > 1 else 0


def _os_row_cell_details(
    row_number: int,
    current_draft_token: str = "",
    *,
    stock_rows: list[dict] | None = None,
    reservation_rows: list[dict] | None = None,
) -> dict[str, dict]:
    if stock_rows is None:
        rows = _active_stockmap_rows(zone="OS", row_number=row_number)
    else:
        rows = [
            row
            for row in stock_rows
            if _normalize_zone(row.get("zone") or "") == "OS"
            and _int_value(row.get("row")) == row_number
        ]
    if reservation_rows is None:
        active_reservations = _active_os_reservation_rows(
            row_number=row_number,
            current_draft_token=current_draft_token,
        )
    else:
        active_reservations = [
            row
            for row in reservation_rows
            if _int_value(row.get("row")) == row_number
        ]
    if not rows and not active_reservations:
        return {}

    grouped: dict[tuple[int, int, int], dict[str, dict]] = {}
    for stock_row in rows:
        section = _int_value(stock_row.get("section"))
        tier = _int_value(stock_row.get("tier"))
        cell = _int_value(stock_row.get("cell"))
        if not section or not tier or not cell:
            continue
        pallet_code = _clean_text(stock_row.get("pallet_code"))
        if not pallet_code:
            continue
        agency_id = int(stock_row.get("agency_id") or 0)
        client_marker = _client_index_marker(
            agency_id,
            agency=stock_row.get("agency"),
            pallet_code=pallet_code,
            sku_code=_clean_text(stock_row.get("sku")),
        )
        cell_bucket = grouped.setdefault((section, tier, cell), {})
        pallet_bucket = cell_bucket.setdefault(
            f"placed:{pallet_code}",
            {
                "pallet_code": pallet_code,
                "display_marker": _stock_display_marker(
                    pallet_code=pallet_code,
                    sku_code=_clean_text(stock_row.get("sku")),
                ),
                "map_marker": client_marker,
                "client_name": _clean_text(getattr(stock_row.get("agency"), "agn_name", "")) or f"Клиент {agency_id}",
                "order_label": _process_label(
                    _clean_text(stock_row.get("order_type")),
                    _clean_text(stock_row.get("order_id")),
                ),
                "location": _clean_text(stock_row.get("location")) or f"OS · {_os_location_code(row=row_number, section=section, tier=tier, cell=cell)}",
                "total_qty": 0,
                "available_qty": 0,
                "processing_reserved_qty": 0,
                "shipping_reserved_qty": 0,
                "box_codes": set(),
                "row_count": 0,
                "items": {},
                "entry_kind": "placed",
                "stage_label": _reservation_stage_label("placed"),
                "is_owned_by_current_draft": False,
            },
        )
        pallet_bucket["total_qty"] += int(stock_row.get("qty") or 0)
        pallet_bucket["available_qty"] += int(stock_row.get("available_qty") or 0)
        pallet_bucket["processing_reserved_qty"] += int(stock_row.get("processing_reserved_qty") or 0)
        pallet_bucket["shipping_reserved_qty"] += int(stock_row.get("shipping_reserved_qty") or 0)
        pallet_bucket["row_count"] += 1
        box_code = _clean_text(stock_row.get("box_code"))
        if box_code:
            pallet_bucket["box_codes"].add(box_code)
        item_key = (
            _clean_text(stock_row.get("sku")),
            _clean_text(stock_row.get("name")),
            _clean_text(stock_row.get("size")),
            _clean_text(stock_row.get("barcode")),
            _clean_text(stock_row.get("goods_type")),
        )
        item_bucket = pallet_bucket["items"].setdefault(
            item_key,
            {
                "sku": item_key[0] or "—",
                "name": item_key[1] or "—",
                "size": item_key[2] or "—",
                "barcode": item_key[3] or "—",
                "goods_type": item_key[4] or "—",
                "qty": 0,
                "boxes": set(),
                "row_count": 0,
            },
        )
        item_bucket["qty"] += int(stock_row.get("qty") or 0)
        item_bucket["row_count"] += 1
        if box_code:
            item_bucket["boxes"].add(box_code)

    for reservation in active_reservations:
        section = _int_value(reservation.get("section"))
        tier = _int_value(reservation.get("tier"))
        cell = _int_value(reservation.get("cell"))
        if not section or not tier or not cell:
            continue
        reservation_kind = _clean_text(reservation.get("reservation_kind")).lower() or "planned"
        pallet_code = _clean_text(reservation.get("pallet_code")) or "Бронь"
        cell_bucket = grouped.setdefault((section, tier, cell), {})
        cell_bucket.setdefault(
            f"{reservation_kind}:{pallet_code}",
            {
                "pallet_code": pallet_code,
                "display_marker": _clean_text(reservation.get("client_marker")) or _stock_display_marker(pallet_code=pallet_code),
                "map_marker": _clean_text(reservation.get("client_marker")),
                "client_name": _clean_text(reservation.get("client_name")) or f"Клиент {reservation.get('agency_id')}",
                "order_label": _process_label(
                    _clean_text(reservation.get("order_type")),
                    _clean_text(reservation.get("order_id")),
                ),
                "location": _clean_text(reservation.get("location")) or f"OS · {_os_location_code(row=row_number, section=section, tier=tier, cell=cell)}",
                "total_qty": 0,
                "available_qty": 0,
                "processing_reserved_qty": 0,
                "shipping_reserved_qty": 0,
                "box_codes": set(reservation.get("box_codes") or []),
                "row_count": int(reservation.get("row_count", 1) or 0),
                "items": {},
                "entry_kind": reservation_kind,
                "stage_label": _clean_text(reservation.get("stage_label")) or _reservation_stage_label(reservation_kind),
                "is_owned_by_current_draft": bool(reservation.get("is_owned_by_current_draft")),
            },
        )

    def _is_dead_placed_pallet(pallet: dict) -> bool:
        if _clean_text(pallet.get("entry_kind")).lower() != "placed":
            return False
        if int(pallet.get("available_qty") or 0) != 0:
            return False
        if int(pallet.get("processing_reserved_qty") or 0) != 0:
            return False
        if int(pallet.get("shipping_reserved_qty") or 0) != 0:
            return False
        if _resolved_box_count(pallet.get("box_codes"), pallet.get("row_count")) != 0:
            return False
        return True

    details: dict[str, dict] = {}
    for (section, tier, cell), pallets in grouped.items():
        pallets = {
            pallet_key: pallet
            for pallet_key, pallet in pallets.items()
            if not _is_dead_placed_pallet(pallet)
        }
        if not pallets:
            continue
        pallet_entries = []
        client_names = []
        for pallet_key in sorted(
            pallets.keys(),
            key=lambda key: (
                0 if pallets[key].get("entry_kind") == "placed" else (1 if pallets[key].get("entry_kind") == "planned" else 2),
                pallets[key].get("pallet_code") or "",
            ),
        ):
            pallet = pallets[pallet_key]
            items = []
            for item in pallet["items"].values():
                items.append(
                    {
                        "sku": item["sku"],
                        "name": item["name"],
                        "size": item["size"],
                        "barcode": item["barcode"],
                        "goods_type": item["goods_type"],
                        "qty": item["qty"],
                        "box_count": _resolved_box_count(item["boxes"], item["row_count"]),
                    }
                )
            items.sort(key=lambda row: (row["sku"], row["size"], row["barcode"]))
            client_names.append(pallet["client_name"])
            pallet_entries.append(
                {
                    "pallet_code": pallet["pallet_code"],
                    "display_marker": pallet["display_marker"],
                    "map_marker": pallet["map_marker"],
                    "client_name": pallet["client_name"],
                    "order_label": pallet["order_label"],
                    "location": pallet["location"],
                    "total_qty": pallet["total_qty"],
                    "available_qty": pallet["available_qty"],
                    "processing_reserved_qty": pallet["processing_reserved_qty"],
                    "shipping_reserved_qty": pallet["shipping_reserved_qty"],
                    "box_count": _resolved_box_count(pallet["box_codes"], pallet["row_count"]),
                    "items": items,
                    "entry_kind": pallet["entry_kind"],
                    "stage_label": pallet["stage_label"],
                    "is_owned_by_current_draft": pallet["is_owned_by_current_draft"],
                }
            )
        client_names = sorted({name for name in client_names if name})
        visual_state = "placed"
        if len(pallet_entries) > 1:
            visual_state = "stacked"
        elif pallet_entries:
            entry_kind = _clean_text(pallet_entries[0].get("entry_kind")).lower()
            if entry_kind == "draft":
                visual_state = "draft_owned" if pallet_entries[0].get("is_owned_by_current_draft") else "draft"
            elif entry_kind == "planned":
                visual_state = "planned"
            else:
                visual_state = "placed"
        details[_os_cell_key(section, tier, cell)] = {
            "cell_label": f"OS · {_os_location_code(row=row_number, section=section, tier=tier, cell=cell)}",
            "summary": " / ".join(client_names) if client_names else f"{len(pallet_entries)} паллет",
            "display_marker": next(
                (entry.get("display_marker") for entry in pallet_entries if _clean_text(entry.get("display_marker"))),
                "",
            ),
            "map_marker": next(
                (
                    _clean_text(entry.get("map_marker")) or _clean_text(entry.get("display_marker"))
                    for entry in pallet_entries
                    if _clean_text(entry.get("map_marker")) or _clean_text(entry.get("display_marker"))
                ),
                "",
            ),
            "visual_state": visual_state,
            "owned_by_current_draft": bool(pallet_entries and pallet_entries[0].get("is_owned_by_current_draft")),
            "pallet_count": len(pallet_entries),
            "pallets": pallet_entries,
        }
    return details


def _location_label(location: dict | None) -> str:
    location = location or {}
    zone = _normalize_zone(location.get("zone") or "") or "PR"
    row = _int_value(location.get("row"))
    section = _int_value(location.get("section"))
    tier = _int_value(location.get("tier"))
    cell = _int_value(location.get("cell"))
    if zone == "PR":
        return "PR · Зона приемки"
    if zone == "OBR":
        return "OBR · Зона обработки"
    if zone == "OTG":
        return "OTG · Зона отгрузки"
    if zone == "MR":
        return f"MR · Между рядами · Ряд {row}" if row else "MR · Между рядами"
    if zone == "OS":
        if row and section and tier and cell:
            return f"OS · {_os_location_code(row=row, section=section, tier=tier, cell=cell)}"
        if row and section:
            return f"OS · {_os_location_code(row=row, section=section)}"
        if row:
            return f"OS · Стеллаж {row}"
        return "OS · Основной склад"
    return zone


def _process_label(order_type: str, order_id: str) -> str:
    token = _clean_text(order_type).lower()
    mapping = {
        "receiving": "Приемка",
        "processing": "Обработка",
        "shipping": "Отгрузка",
        "stock_move": "Перемещение",
        "manual": "Ручной запрос",
    }
    prefix = mapping.get(token, order_type or "Процесс")
    order_text = _clean_text(order_id)
    return f"{prefix} #{order_text}" if order_text else prefix


def _latest_tasks_by_pallet(pallet_codes: list[str]) -> dict[str, MoveTask]:
    codes = [str(code or "").strip() for code in pallet_codes if str(code or "").strip()]
    if not codes:
        return {}
    latest: dict[str, MoveTask] = {}
    for task in MoveTask.objects.filter(pallet_code__in=codes).order_by("-created_at"):
        code = _clean_text(task.pallet_code)
        if not code or code in latest:
            continue
        latest[code] = task
    return latest


def _suggest_os_destinations_for_pr(rows: list[dict], *, destinations_override: dict[str, dict] | None = None) -> dict[str, dict]:
    override = destinations_override if isinstance(destinations_override, dict) else {}
    occupied_os = set(StockAvailabilityService.occupied_os_cell_keys())
    section_agencies = StockAvailabilityService.occupied_os_section_agencies()
    used_os: set[tuple[int, int, int, int]] = set()
    result: dict[str, dict] = {}
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            _clean_text(getattr(row.get("agency"), "agn_name", "")),
            _clean_text(row.get("pallet_code")),
        ),
    )
    for row in sorted_rows:
        pallet_code = _clean_text(row.get("pallet_code"))
        if not pallet_code:
            continue
        explicit = override.get(pallet_code)
        if explicit:
            os_key = (
                _int_value(explicit.get("row")),
                _int_value(explicit.get("section")),
                _int_value(explicit.get("tier")),
                _int_value(explicit.get("cell")),
            )
            if all(os_key):
                used_os.add(os_key)
                agency = row.get("agency")
                if agency and os_key[0] and os_key[1]:
                    section_agencies.setdefault((os_key[0], os_key[1]), set()).add(int(agency.id))
                result[pallet_code] = explicit
                continue
        agency = row.get("agency")
        assigned = StockAvailabilityService.suggest_os_cell_for_agency(
            agency_id=int(agency.id) if agency else None,
            row_sections=_OS_ROW_SECTIONS,
            tiers=_OS_TIERS,
            cells_per_tier=_OS_CELLS_PER_TIER,
            occupied_keys=occupied_os,
            used_cell_keys=used_os,
            section_agencies=section_agencies,
        )
        if not assigned:
            continue
        os_key = (
            _int_value(assigned.get("row")),
            _int_value(assigned.get("section")),
            _int_value(assigned.get("tier")),
            _int_value(assigned.get("cell")),
        )
        if all(os_key):
            used_os.add(os_key)
            if agency and os_key[0] and os_key[1]:
                section_agencies.setdefault((os_key[0], os_key[1]), set()).add(int(agency.id))
        result[pallet_code] = assigned
    return result


def _pr_zone_rows(*, destinations_override: dict[str, dict] | None = None) -> list[dict]:
    rows = _active_stockmap_rows(zone="PR")
    grouped: dict[str, dict] = {}
    for stock_row in rows:
        pallet_code = _clean_text(stock_row.get("pallet_code"))
        if not pallet_code:
            continue
        bucket = grouped.setdefault(
            pallet_code,
            {
                "pallet_code": pallet_code,
                "agency": stock_row.get("agency"),
                "agency_id": int(stock_row.get("agency_id") or 0),
                "client_name": _clean_text(getattr(stock_row.get("agency"), "agn_name", "")) or f"Клиент {stock_row.get('agency_id')}",
                "order_type": _clean_text(stock_row.get("order_type")),
                "order_id": _clean_text(stock_row.get("order_id")),
                "total_qty": 0,
                "available_qty": 0,
                "processing_reserved_qty": 0,
                "shipping_reserved_qty": 0,
                "box_codes": set(),
                "row_count": 0,
                "items": {},
            },
        )
        bucket["total_qty"] += int(stock_row.get("qty") or 0)
        bucket["available_qty"] += int(stock_row.get("available_qty") or 0)
        bucket["processing_reserved_qty"] += int(stock_row.get("processing_reserved_qty") or 0)
        bucket["shipping_reserved_qty"] += int(stock_row.get("shipping_reserved_qty") or 0)
        bucket["row_count"] += 1
        box_code = _clean_text(stock_row.get("box_code"))
        if box_code:
            bucket["box_codes"].add(box_code)
        item_key = (
            _clean_text(stock_row.get("sku")),
            _clean_text(stock_row.get("name")),
            _clean_text(stock_row.get("size")),
            _clean_text(stock_row.get("barcode")),
        )
        item_bucket = bucket["items"].setdefault(
            item_key,
            {
                "sku": item_key[0] or "—",
                "name": item_key[1] or "—",
                "size": item_key[2] or "—",
                "barcode": item_key[3] or "—",
                "qty": 0,
            },
        )
        item_bucket["qty"] += int(stock_row.get("qty") or 0)

    result = []
    latest_tasks = _latest_tasks_by_pallet(list(grouped.keys()))
    destinations = _suggest_os_destinations_for_pr(list(grouped.values()), destinations_override=destinations_override)
    for pallet_code in sorted(grouped.keys()):
        bucket = grouped[pallet_code]
        task = latest_tasks.get(pallet_code)
        task_status = _clean_text(getattr(task, "status", "")).lower()
        has_active_task = task_status in {MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS}
        destination = destinations.get(pallet_code)
        can_move_to_os = bool(destination and not has_active_task)
        if has_active_task:
            can_move_reason = "Уже есть активное задание ричтрака."
        elif not destination:
            can_move_reason = "Нет свободной ячейки на основном складе."
        else:
            can_move_reason = "Можно создать задание ричтраку."
        task_label = "Нет заданий"
        if task is not None:
            task_label = {
                MoveTask.STATUS_CREATED: f"Задание #{task.legacy_order_id or task.id} создано",
                MoveTask.STATUS_IN_PROGRESS: f"Задание #{task.legacy_order_id or task.id} в работе",
                MoveTask.STATUS_DONE: f"Задание #{task.legacy_order_id or task.id} выполнено",
                MoveTask.STATUS_CANCELED: f"Задание #{task.legacy_order_id or task.id} отменено",
                MoveTask.STATUS_FAILED: f"Задание #{task.legacy_order_id or task.id} с ошибкой",
            }.get(task_status, f"Задание #{task.legacy_order_id or task.id}")
        process_bits = []
        if bucket["processing_reserved_qty"] > 0:
            process_bits.append(f"OBR резерв {bucket['processing_reserved_qty']} шт.")
        if bucket["shipping_reserved_qty"] > 0:
            process_bits.append(f"OTG резерв {bucket['shipping_reserved_qty']} шт.")
        process_hint = "; ".join(process_bits) if process_bits else "Активных резервов нет."
        items = sorted(bucket["items"].values(), key=lambda item: (item["sku"], item["size"], item["barcode"]))
        items_summary = ", ".join(
            f"{item['sku']} {item['size']} · {item['qty']} шт."
            for item in items[:3]
        )
        if len(items) > 3:
            items_summary = f"{items_summary}, еще {len(items) - 3} поз."
        result.append(
            {
                "pallet_code": pallet_code,
                "agency": bucket["agency"],
                "agency_id": bucket["agency_id"],
                "client_name": bucket["client_name"],
                "order_type": bucket["order_type"],
                "order_id": bucket["order_id"],
                "source_label": _process_label(bucket["order_type"], bucket["order_id"]),
                "total_qty": bucket["total_qty"],
                "available_qty": bucket["available_qty"],
                "processing_reserved_qty": bucket["processing_reserved_qty"],
                "shipping_reserved_qty": bucket["shipping_reserved_qty"],
                "box_count": _resolved_box_count(bucket["box_codes"], bucket["row_count"]),
                "items": items,
                "items_summary": items_summary or "Состав не найден",
                "process_hint": process_hint,
                "latest_task_label": task_label,
                "destination": destination or {},
                "destination_label": _location_label(destination) if destination else "Нет свободной ячейки OS",
                "can_move_to_os": can_move_to_os,
                "can_move_reason": can_move_reason,
            }
        )
    return result


def _os_row_badge_style(*, occupied: int, total: int) -> dict:
    total_slots = max(0, int(total or 0))
    occupied_slots = min(max(0, int(occupied or 0)), total_slots) if total_slots else 0
    ratio = 0.0 if total_slots <= 0 else occupied_slots / total_slots
    shadow_alpha = 0.08 + (ratio * 0.22)
    return {
        "fill_ratio": ratio,
        "fill_percent": int(round(ratio * 100)),
        "badge_bg": _rgb_css(_blend_rgb(_OS_ROW_BADGE_BG_START, _OS_ROW_BADGE_BG_END, ratio)),
        "badge_border": _rgb_css(_blend_rgb(_OS_ROW_BADGE_BORDER_START, _OS_ROW_BADGE_BORDER_END, ratio)),
        "badge_ink": _rgb_css(_blend_rgb(_OS_ROW_BADGE_INK_START, _OS_ROW_BADGE_INK_END, ratio)),
        "badge_shadow": (
            f"0 10px 24px rgba({_OS_ROW_BADGE_SHADOW_RGB[0]}, "
            f"{_OS_ROW_BADGE_SHADOW_RGB[1]}, {_OS_ROW_BADGE_SHADOW_RGB[2]}, {shadow_alpha:.2f})"
        ),
    }


def _blend_rgb(start: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    clamped = max(0.0, min(1.0, float(ratio or 0.0)))
    return tuple(
        int(round(start[channel] + ((end[channel] - start[channel]) * clamped)))
        for channel in range(3)
    )


def _rgb_css(rgb: tuple[int, int, int]) -> str:
    red, green, blue = rgb
    return f"rgb({red}, {green}, {blue})"
