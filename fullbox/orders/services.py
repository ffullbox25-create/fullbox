from __future__ import annotations

import json
from copy import deepcopy
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.core.cache import cache
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from agent.models import AgentContext, DeviceAgent
from audit.models import OrderAuditEntry, log_order_action, log_staff_overaction
from employees.models import Employee
from employees.access import get_employee_roles, is_developer_login
from fullbox.container_codes import rewrite_container_code_goods_type
from fullbox.order_numbers import format_order_number
from fullbox.timeutils import parse_business_datetime
from marking.models import MarkingCode
from orders.shipping_returns import (
    SHIPPING_RETURN_GOODS_TYPE,
    SHIPPING_RETURN_GOODS_TYPE_LABEL,
    resolve_shipping_return_order,
    shipping_return_item_key,
    shipping_return_source_items,
    validate_shipping_return_items,
)
from orders.title_truth import build_order_runtime_payload, resolve_order_title
from reachtruck.services.putaway_planner import (
    build_putaway_rows,
    normalize_putaway_location,
    parse_putaway_destinations,
    suggest_putaway_destinations,
)
from sklad.models import (
    InventoryState,
    StockPalletState,
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services.putaway_draft_reservations import PutawayDraftReservationService
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_policy import WarehouseActionPolicy
from sklad.services.warehouse_state import WarehouseGoodsStateResolver, WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sklad.location_occupancy import occupied_operational_location_ids
from sklad.services.operational_locations import receiving_locations
from sklad.topology import os_location_code
from sku.models import Agency, SKU, SKUBarcode
from sklad.services.warehouse_commands import WarehouseCommandService
from todo.models import Task


_IP_PREFIX_RE = re.compile(r"\bиндивидуальный предприниматель\b", re.IGNORECASE)


def _manager_due_date(submitted_at):
    cutoff = submitted_at.replace(hour=14, minute=0, second=0, microsecond=0)
    if submitted_at <= cutoff:
        return submitted_at.replace(hour=18, minute=0, second=0, microsecond=0)
    next_day = submitted_at + timedelta(days=1)
    return next_day.replace(hour=13, minute=0, second=0, microsecond=0)


@dataclass
class ReceivingWorkflowResult:
    act_payload: dict
    placement_payload: dict
    placement_previously_closed: bool
    storekeeper_tasks_closed: int = 0
    manager_followup_created: bool = False
    already_completed: bool = False


@dataclass
class ReceivingDispatchResult:
    payload: dict
    manager_tasks_closed: int = 0
    manager_task_created: bool = False
    storekeeper_task_created: bool = False


@dataclass
class ReceivingSubmissionResult:
    order_id: str
    payload: dict
    status_value: str
    status_label: str
    was_update: bool = False
    review_dispatched: bool = False


@dataclass
class ReceivingStatusUpdateResult:
    applied: bool
    payload: dict
    description: str = ""
    manager_tasks_closed: int = 0


@dataclass
class ReceivingActionResult:
    status: str
    payload: dict = field(default_factory=dict)
    reason: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class ReceivingFlowPreparationResult:
    status: str
    status_payload: dict = field(default_factory=dict)
    receiving_mode: str = "standard"
    boxes: list[dict] = field(default_factory=list)
    pallets: list[dict] = field(default_factory=list)
    act_items: list[dict] = field(default_factory=list)
    placement_items: list[dict] = field(default_factory=list)
    act_units: list[dict] = field(default_factory=list)
    flow_state: dict = field(default_factory=dict)
    has_mismatch: bool = False
    normalized_eta: str = ""
    vehicle_number: str = ""
    has_closed_placement_act: bool = False
    reason: str = ""


@dataclass
class ReceivingWarehouseMoveResult:
    status: str
    reason: str = ""
    error_message: str = ""
    created_count: int = 0
    skipped_existing_count: int = 0
    skipped_missing_destination_count: int = 0
    total_count: int = 0
    destinations_by_pallet: dict[str, dict] = field(default_factory=dict)
    source_facts: list[str] = field(default_factory=list)


@dataclass
class ReceivingWarehouseMovePanelResult:
    progress: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    can_send: bool = False


@dataclass
class ReceivingPlacementPreparationResult:
    status: str
    placement_items: list[dict] = field(default_factory=list)
    boxes: list[dict] = field(default_factory=list)
    pallets: list[dict] = field(default_factory=list)
    has_closed_act: bool = False
    reason: str = ""


@dataclass
class ReceivingFlowBoxActionResult:
    status: str
    action_kind: str = ""
    action_label: str = ""
    snapshot: dict = field(default_factory=dict)
    reason: str = ""


def _format_payload_value(value):
    if value is None or value == "":
        return "-"
    return str(value).strip()


def _place_type_label(value: str) -> str:
    text = _format_payload_value(value)
    if text == "-":
        return text
    labels = {
        "pallet": "Паллет",
        "box": "Короб",
        "bag": "Мешок",
    }
    return labels.get(text, text)


def _format_datetime_value(value):
    text = _format_payload_value(value)
    if text == "-":
        return text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    return parsed.strftime("%d.%m.%Y, %H:%M")


def _describe_payload_changes(old_payload, new_payload):
    changes = []
    fields = [
        ("eta_at", "Плановая дата/время"),
        ("expected_boxes", "Количество мест"),
        ("place_type", "Тип мест"),
        ("vehicle_number", "Номер авто"),
        ("driver_phone", "Телефон водителя"),
        ("receiving_chz_service", "Сканирование Честного знака"),
        ("receiving_route", "Маршрут после приемки"),
        ("comment", "Комментарий"),
    ]
    for key, label in fields:
        if key == "eta_at":
            old_val = _format_datetime_value((old_payload or {}).get(key))
            new_val = _format_datetime_value((new_payload or {}).get(key))
        elif key == "place_type":
            old_val = _place_type_label((old_payload or {}).get(key))
            new_val = _place_type_label((new_payload or {}).get(key))
        elif key == "receiving_chz_service":
            old_val = ReceivingWorkflowService.receiving_chz_service_label(
                (old_payload or {}).get(key)
            ) or "-"
            new_val = ReceivingWorkflowService.receiving_chz_service_label(
                (new_payload or {}).get(key)
            ) or "-"
        elif key == "receiving_route":
            old_val = ReceivingWorkflowService.receiving_route_label(
                (old_payload or {}).get(key)
            )
            new_val = ReceivingWorkflowService.receiving_route_label(
                (new_payload or {}).get(key)
            )
        else:
            old_val = _format_payload_value((old_payload or {}).get(key))
            new_val = _format_payload_value((new_payload or {}).get(key))
        if old_val != new_val:
            changes.append(f"{label}: {old_val} → {new_val}")
    old_items = (old_payload or {}).get("items") or []
    new_items = (new_payload or {}).get("items") or []
    if old_items != new_items:
        changes.append(f"Состав поставки: {len(old_items)} → {len(new_items)} поз.")
    return changes


class ReceivingNomenclatureError(ValueError):
    """Validation error for receiving-side nomenclature upserts."""


class ReceivingWorkflowService:
    _WAREHOUSE_CANCEL_REVIEW_ROLES = {"storekeeper", "processing_head"}
    PALLET_PLACEMENT_PERMISSION_EVENT = "receiving_pallet_placement_permission"
    # Остаток по паллете создается один раз — в момент разрешения размещения.
    # Строки остатка исчезают, когда товар уезжает (архив с qty=0), поэтому
    # признак оприходования живет в журнале приемки, а не в самих строках.
    PALLET_STOCK_MATERIALIZED_EVENT = "receiving_pallet_stock_materialized"

    INVALID_RECEIVING_REASON_LABELS = {
        "duplicate": "Дубль заявки",
        "wrong_request": "Неверная заявка",
        "created_by_mistake": "Создана ошибочно",
    }

    RECEIVING_GOODS_TYPE_LABELS = {
        "op": "Оптовый",
        "gv": "Готовый",
        "br": "Брак",
        "vz": "Возврат",
        "rh": "Расходный",
        "no": "Не обработанный",
        SHIPPING_RETURN_GOODS_TYPE: SHIPPING_RETURN_GOODS_TYPE_LABEL,
    }
    BOX_METRIC_FIELDS = ("gross_weight_g", "width_mm", "height_mm", "depth_mm")
    BOX_METRIC_HISTORY_LOOKBACK = timedelta(days=15)
    BOX_METRIC_HISTORY_ENTRY_LIMIT = 400
    RECEIVING_MODES = {"standard", "cz"}
    RECEIVING_CHZ_SERVICE_LABELS = {
        "required": "Сканировать Честный знак",
        "not_required": "Честный знак по выбору приемщика",
    }
    RECEIVING_ROUTE_LABELS = {
        "fbo": "FBO",
        "fbs": "FBS",
    }
    FLOW_BOX_ACTIONS = {
        "edit": ("update", "Редактирование короба"),
        "delete": ("delete", "Удаление короба"),
        "delete_batch": ("delete", "Удаление группы коробов"),
        "move_batch": ("update", "Перемещение группы коробов"),
        "print_batch": ("update", "Печать этикеток группы коробов"),
    }

    @staticmethod
    def requires_concrete_receiving_location(entries) -> bool:
        return any(
            bool((entry.payload or {}).get("receiving_concrete_location_required"))
            for entry in entries or []
            if isinstance(entry.payload, dict)
        )

    @classmethod
    def _normalize_receiving_mode_for_goods_type(cls, receiving_mode: str, goods_type: str) -> str:
        normalized_mode = str(receiving_mode or "standard").strip().lower()
        if normalized_mode not in cls.RECEIVING_MODES:
            normalized_mode = "standard"
        if str(goods_type or "").strip().lower() == "no":
            return "standard"
        return normalized_mode

    @classmethod
    def normalize_receiving_chz_service(cls, value) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in cls.RECEIVING_CHZ_SERVICE_LABELS else ""

    @classmethod
    def receiving_chz_service_label(cls, value) -> str:
        return cls.RECEIVING_CHZ_SERVICE_LABELS.get(
            cls.normalize_receiving_chz_service(value),
            "",
        )

    @classmethod
    def normalize_receiving_route(cls, value, *, default: str = "") -> str:
        normalized = str(value or "").strip().lower()
        if normalized in cls.RECEIVING_ROUTE_LABELS:
            return normalized
        return default if default in cls.RECEIVING_ROUTE_LABELS else ""

    @classmethod
    def receiving_route_label(cls, value) -> str:
        # Legacy requests were always routed through the ordinary FBO contour.
        normalized = cls.normalize_receiving_route(value, default="fbo")
        return cls.RECEIVING_ROUTE_LABELS[normalized]

    @classmethod
    def receiving_route_from_entries(cls, entries) -> str:
        for entry in reversed(list(entries or [])):
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if "receiving_route" in payload:
                return cls.normalize_receiving_route(
                    payload.get("receiving_route"),
                    default="fbo",
                )
        return "fbo"

    @classmethod
    def receiving_route_constraint_error(
        cls,
        *,
        payload: dict | None,
        goods_type: str,
    ) -> str:
        route = cls.normalize_receiving_route(
            (payload or {}).get("receiving_route"),
            default="fbo",
        )
        if route == "fbs" and str(goods_type or "").strip().lower() != "gv":
            return "Маршрут FBS доступен только для готового товара типа GV."
        return ""

    @classmethod
    def receiving_mode_required_by_client(cls, payload: dict | None) -> str:
        service = cls.normalize_receiving_chz_service(
            (payload or {}).get("receiving_chz_service")
        )
        if service == "required":
            return "cz"
        return ""

    @classmethod
    def effective_receiving_mode(cls, payload: dict | None) -> str:
        """Resolve the operational mode, including old requests without a saved mode."""
        source = payload if isinstance(payload, dict) else {}
        requested_mode = (
            cls.receiving_mode_required_by_client(source)
            or str(source.get("receiving_mode") or "").strip().lower()
            or "standard"
        )
        return cls._normalize_receiving_mode_for_goods_type(
            requested_mode,
            str(source.get("goods_type") or "").strip().lower(),
        )

    @classmethod
    def receiving_mode_constraint_error(
        cls,
        *,
        payload: dict | None,
        goods_type: str,
        receiving_mode: str,
    ) -> str:
        required_mode = cls.receiving_mode_required_by_client(payload)
        if not required_mode:
            return ""
        normalized_goods_type = str(goods_type or "").strip().lower()
        normalized_mode = str(receiving_mode or "").strip().lower()
        if required_mode == "cz" and normalized_goods_type == "no":
            return (
                "Клиент заказал сканирование Честного знака. "
                "Тип поставки «Не обработанный» для этой заявки недоступен."
            )
        if normalized_mode != required_mode:
            return (
                "Для этой заявки клиент выбрал приемку с Честным знаком."
                if required_mode == "cz"
                else "Для этой заявки клиент выбрал приемку без Честного знака."
            )
        return ""
    FLOW_PAYLOAD_IGNORED_KEYS = {
        "comment",
        "message",
        "status",
        "status_label",
        "submit_action",
        "flow_state",
        "flow_boxes",
        "flow_pallets",
        "flow_active_box",
        "flow_active_pallet",
        "packing_assignee",
        "packing_assignee_id",
        "packing_assignee_role",
    }
    FLOW_PAYLOAD_FALLBACK_IGNORED_KEYS = {
        "comment",
        "message",
        "flow_state",
        "flow_boxes",
        "flow_pallets",
        "flow_active_box",
        "flow_active_pallet",
        "packing_assignee",
        "packing_assignee_id",
        "packing_assignee_role",
    }

    @staticmethod
    def _authenticated_user(user):
        return user if getattr(user, "is_authenticated", False) else None

    @staticmethod
    def _parse_qty_value(raw: str | None) -> int | None:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        try:
            return int(text)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_positive_int(raw) -> int | None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _item_key(
        sku: str | None,
        name: str | None,
        size: str | None,
        brand: str | None = None,
        color: str | None = None,
    ) -> str:
        sku_part = (sku or "").strip().lower()
        name_part = (name or "").strip().lower()
        brand_part = (brand or "").strip().lower()
        color_part = (color or "").strip().lower()
        size_part = (size or "").strip().lower()
        return f"{sku_part}|{name_part}|{brand_part}|{color_part}|{size_part}"

    @classmethod
    def _extract_box_metric_snapshot(cls, box: dict | None) -> dict[str, int] | None:
        if not isinstance(box, dict):
            return None
        values: dict[str, int] = {}
        for field in cls.BOX_METRIC_FIELDS:
            numeric = cls._parse_positive_int(box.get(field))
            if numeric is None:
                return None
            values[field] = numeric
        return values

    @staticmethod
    def _box_metric_item_barcode(item: dict | None) -> str:
        if not isinstance(item, dict):
            return ""
        return str(
            item.get("barcode")
            or item.get("shk")
            or item.get("barcode_value")
            or ""
        ).strip().lower()

    @staticmethod
    def _box_metric_agency_key(agency: Agency | None = None, agency_id=None) -> str:
        value = agency_id
        if value in (None, "") and agency is not None:
            value = getattr(agency, "id", "")
        return str(value or "").strip().lower()

    @classmethod
    def _build_box_metric_barcode_qty_key(
        cls,
        box: dict | None,
        *,
        agency: Agency | None = None,
        agency_id=None,
    ) -> str:
        if not isinstance(box, dict):
            return ""
        agency_key = cls._box_metric_agency_key(agency=agency, agency_id=agency_id)
        grouped: dict[str, int] = {}
        for item in box.get("items") or []:
            if not isinstance(item, dict):
                continue
            qty = cls._parse_qty_value(item.get("qty")) or 0
            barcode = cls._box_metric_item_barcode(item)
            if qty <= 0 or not barcode:
                continue
            item_key = f"{agency_key}|{barcode}" if agency_key else barcode
            grouped[item_key] = int(grouped.get(item_key) or 0) + qty
        if not grouped:
            return ""
        return json.dumps(
            sorted(grouped.items(), key=lambda item: item[0]),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def _box_metric_target_barcodes(
        cls,
        *,
        items: list[dict] | None = None,
        flow_state: dict | None = None,
    ) -> set[str]:
        values = {
            cls._box_metric_item_barcode(item)
            for item in (items or [])
            if isinstance(item, dict)
        }
        if isinstance(flow_state, dict):
            for box in flow_state.get("boxes") or []:
                if not isinstance(box, dict):
                    continue
                values.update(
                    cls._box_metric_item_barcode(item)
                    for item in (box.get("items") or [])
                    if isinstance(item, dict)
                )
        return {value for value in values if value}

    @classmethod
    def _warehouse_box_metrics_defaults(
        cls,
        *,
        agency: Agency | None,
        barcode_values: set[str],
    ) -> dict[str, dict[str, int]]:
        if not agency or not barcode_values:
            return {}
        barcode_filter = models.Q()
        for value in barcode_values:
            barcode_filter |= models.Q(barcode__iexact=value)
        if not barcode_filter:
            return {}
        matching_container_ids = list(
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                is_archived=False,
                qty__gt=0,
                container__isnull=False,
                container__container_type="box",
            )
            .filter(barcode_filter)
            .values_list("container_id", flat=True)
            .distinct()
        )
        if not matching_container_ids:
            return {}
        snapshots = (
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                is_archived=False,
                qty__gt=0,
                container_id__in=matching_container_ids,
                container__gross_weight_g__gt=0,
                container__width_mm__gt=0,
                container__height_mm__gt=0,
                container__depth_mm__gt=0,
            )
            .select_related("container")
            .order_by("-container__updated_at", "-updated_at", "-id")
        )
        boxes: dict[int, dict] = {}
        for snapshot in snapshots:
            container = snapshot.container
            if container is None:
                continue
            container_id = int(container.id or 0)
            if not container_id:
                continue
            box = boxes.setdefault(
                container_id,
                {
                    "metrics": {
                        "gross_weight_g": int(container.gross_weight_g or 0),
                        "width_mm": int(container.width_mm or 0),
                        "height_mm": int(container.height_mm or 0),
                        "depth_mm": int(container.depth_mm or 0),
                    },
                    "items": [],
                },
            )
            box["items"].append(
                {
                    "barcode": snapshot.barcode,
                    "qty": int(snapshot.qty or 0),
                }
            )
        result: dict[str, dict[str, int]] = {}
        for box in boxes.values():
            lookup_key = cls._build_box_metric_barcode_qty_key(box, agency=agency)
            if lookup_key and lookup_key != "[]" and lookup_key not in result:
                result[lookup_key] = box["metrics"]
        return result

    @classmethod
    def _build_box_metric_composition_key(cls, box: dict | None, *, mode: str = "receiving") -> str:
        del mode
        return cls._build_box_metric_barcode_qty_key(box)

    @classmethod
    def _build_box_metric_lookup_key(cls, box: dict | None, *, mode: str = "receiving") -> str:
        del mode
        return cls._build_box_metric_barcode_qty_key(box)

    @classmethod
    def build_known_box_metrics_defaults(
        cls,
        *,
        agency: Agency | None,
        mode: str = "receiving",
        items: list[dict] | None = None,
        flow_state: dict | None = None,
    ) -> dict[str, dict[str, int]]:
        del mode
        if not agency:
            return {}
        barcode_values = cls._box_metric_target_barcodes(items=items, flow_state=flow_state)
        return cls._warehouse_box_metrics_defaults(
            agency=agency,
            barcode_values=barcode_values,
        )

    @classmethod
    def _latest_payload(cls, entries) -> dict:
        return build_order_runtime_payload("receiving", entries=entries)

    @staticmethod
    def _parse_flow_client_version(value) -> int:
        try:
            parsed = int(str(value or "").strip())
        except (TypeError, ValueError):
            return 0
        return parsed if parsed > 0 else 0

    @classmethod
    def _latest_flow_client_version(cls, entries) -> int:
        entry_list = list(entries or [])
        latest_flow_boundary = -1
        for index, entry in enumerate(entry_list):
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if payload.get("flow_closed") or payload.get("flow_reopened"):
                latest_flow_boundary = index
        for entry in reversed(entry_list[latest_flow_boundary + 1 :]):
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if entry.action == "update" and isinstance(payload.get("flow_state"), dict):
                return cls._parse_flow_client_version(payload.get("flow_client_version"))
        return 0

    @classmethod
    def _latest_receiving_flow_draft_entry(cls, entries):
        entry_list = list(entries or [])
        latest_flow_boundary = -1
        for index, entry in enumerate(entry_list):
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if payload.get("flow_closed") or payload.get("flow_reopened"):
                latest_flow_boundary = index
        for entry in reversed(entry_list[latest_flow_boundary + 1 :]):
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if entry.action == "update" and isinstance(payload.get("flow_state"), dict):
                return entry
        return None

    @classmethod
    def _migrate_receiving_flow_draft_goods_type(
        cls,
        *,
        entries,
        order_id: str,
        previous_goods_type: str,
        next_goods_type: str,
    ) -> dict:
        previous_type = cls._normalize_flow_goods_type(previous_goods_type)
        next_type = cls._normalize_flow_goods_type(next_goods_type)
        result = {
            "draft_found": False,
            "boxes_updated": 0,
            "pallets_updated": 0,
            "box_codes_updated": 0,
            "pallet_codes_updated": 0,
            "marking_codes_updated": 0,
            "flow_client_version": 0,
        }
        if not previous_type or not next_type or previous_type == next_type:
            return result

        draft_entry = cls._latest_receiving_flow_draft_entry(entries)
        if not draft_entry:
            return result
        result["draft_found"] = True

        draft_payload = dict(draft_entry.payload or {})
        source_flow_state = draft_payload.get("flow_state")
        if not isinstance(source_flow_state, dict):
            return result
        flow_state = dict(source_flow_state)

        source_boxes = source_flow_state.get("boxes")
        boxes = []
        box_code_map: dict[str, str] = {}
        active_box_code = str(source_flow_state.get("activeBox") or "").strip()

        def items_have_goods(raw_items) -> bool:
            for raw_item in raw_items if isinstance(raw_items, list) else []:
                if not isinstance(raw_item, dict):
                    continue
                try:
                    if float(raw_item.get("qty") or 0) > 0:
                        return True
                except (TypeError, ValueError):
                    continue
            return False

        box_has_goods_by_code = {
            str(box.get("code") or "").strip(): items_have_goods(box.get("items"))
            for box in (source_boxes if isinstance(source_boxes, list) else [])
            if isinstance(box, dict) and str(box.get("code") or "").strip()
        }
        occupied_box_codes = {
            str(box.get("code") or "").strip().casefold()
            for box in (source_boxes if isinstance(source_boxes, list) else [])
            if isinstance(box, dict) and str(box.get("code") or "").strip()
        }
        for source_box in source_boxes if isinstance(source_boxes, list) else []:
            if not isinstance(source_box, dict):
                continue
            box = dict(source_box)
            source_items = source_box.get("items")
            items = []
            explicit_type = cls._normalize_flow_goods_type(source_box.get("goods_type"))
            previous_code = str(source_box.get("code") or "").strip()
            # Changing the request type starts a new receiving segment. Only an
            # unused active box may follow the new type; accepted goods keep the
            # type under which they were received.
            inherits_previous_type = (
                previous_code == active_box_code
                and not bool(source_box.get("sealed"))
                and not box_has_goods_by_code.get(previous_code, False)
                and (not explicit_type or explicit_type == previous_type)
            )
            if inherits_previous_type:
                box["goods_type"] = next_type
                result["boxes_updated"] += 1
            for source_item in source_items if isinstance(source_items, list) else []:
                if not isinstance(source_item, dict):
                    continue
                item = dict(source_item)
                item_type = cls._normalize_flow_goods_type(source_item.get("goods_type"))
                if inherits_previous_type and (not item_type or item_type == previous_type):
                    item["goods_type"] = next_type
                items.append(item)
            if isinstance(source_items, list):
                box["items"] = items

            if inherits_previous_type and previous_code:
                code_parts = previous_code.rsplit("-", 1)
                if len(code_parts) == 2 and code_parts[-1].strip().lower() == previous_type:
                    next_code = rewrite_container_code_goods_type(previous_code, next_type)
                    next_key = next_code.casefold()
                    previous_key = previous_code.casefold()
                    if next_code and next_code != previous_code and (
                        next_key == previous_key or next_key not in occupied_box_codes
                    ):
                        occupied_box_codes.discard(previous_key)
                        occupied_box_codes.add(next_key)
                        box["code"] = next_code
                        box_code_map[previous_code] = next_code
                        result["box_codes_updated"] += 1
            boxes.append(box)
        if isinstance(source_boxes, list):
            flow_state["boxes"] = boxes

        source_pallets = source_flow_state.get("pallets")
        pallets = []
        pallet_code_map: dict[str, str] = {}
        active_pallet_code = str(source_flow_state.get("activePallet") or "").strip()
        occupied_pallet_codes = {
            str(pallet.get("code") or "").strip().casefold()
            for pallet in (source_pallets if isinstance(source_pallets, list) else [])
            if isinstance(pallet, dict) and str(pallet.get("code") or "").strip()
        }
        for source_pallet in source_pallets if isinstance(source_pallets, list) else []:
            if not isinstance(source_pallet, dict):
                continue
            pallet = dict(source_pallet)
            explicit_type = cls._normalize_flow_goods_type(source_pallet.get("goods_type"))
            previous_code = str(source_pallet.get("code") or "").strip()
            source_items = source_pallet.get("items")
            source_box_codes = source_pallet.get("boxes")
            pallet_has_goods = items_have_goods(source_items) or any(
                box_has_goods_by_code.get(str(box_code or "").strip(), False)
                for box_code in (source_box_codes if isinstance(source_box_codes, list) else [])
            )
            # A pallet that already contains goods can legitimately become
            # mixed. Keep its original identity and type; only a completely
            # empty active pallet follows the newly selected type.
            inherits_previous_type = (
                previous_code == active_pallet_code
                and not bool(source_pallet.get("sealed"))
                and not pallet_has_goods
                and (not explicit_type or explicit_type == previous_type)
            )
            if inherits_previous_type:
                pallet["goods_type"] = next_type
                result["pallets_updated"] += 1

            items = []
            for source_item in source_items if isinstance(source_items, list) else []:
                if not isinstance(source_item, dict):
                    continue
                item = dict(source_item)
                item_type = cls._normalize_flow_goods_type(source_item.get("goods_type"))
                if inherits_previous_type and (not item_type or item_type == previous_type):
                    item["goods_type"] = next_type
                items.append(item)
            if isinstance(source_items, list):
                pallet["items"] = items
            if isinstance(source_box_codes, list):
                pallet["boxes"] = [
                    box_code_map.get(str(box_code or "").strip(), box_code)
                    for box_code in source_box_codes
                ]

            if inherits_previous_type and previous_code:
                code_parts = previous_code.rsplit("-", 1)
                if len(code_parts) == 2 and code_parts[-1].strip().lower() == previous_type:
                    next_code = rewrite_container_code_goods_type(previous_code, next_type)
                    next_key = next_code.casefold()
                    previous_key = previous_code.casefold()
                    if next_code and next_code != previous_code and (
                        next_key == previous_key or next_key not in occupied_pallet_codes
                    ):
                        occupied_pallet_codes.discard(previous_key)
                        occupied_pallet_codes.add(next_key)
                        pallet["code"] = next_code
                        pallet_code_map[previous_code] = next_code
                        result["pallet_codes_updated"] += 1
            pallets.append(pallet)
        if isinstance(source_pallets, list):
            flow_state["pallets"] = pallets

        active_box = str(source_flow_state.get("activeBox") or "").strip()
        active_pallet = str(source_flow_state.get("activePallet") or "").strip()
        if active_box in box_code_map:
            flow_state["activeBox"] = box_code_map[active_box]
        if active_pallet in pallet_code_map:
            flow_state["activePallet"] = pallet_code_map[active_pallet]

        previous_version = cls._parse_flow_client_version(draft_payload.get("flow_client_version"))
        next_version = previous_version + 1
        draft_payload["flow_state"] = flow_state
        draft_payload["flow_client_version"] = next_version
        draft_entry.payload = draft_payload
        draft_entry.save(update_fields=["payload"])
        result["flow_client_version"] = next_version

        order_key = str(order_id or "").strip()
        for previous_code, next_code in box_code_map.items():
            result["marking_codes_updated"] += MarkingCode.objects.filter(
                order_type="receiving",
                order_id=order_key,
                box_barcode=previous_code,
            ).update(box_barcode=next_code)
        return result

    @staticmethod
    def _barcode_value_for_sku(sku, size: str | None) -> str:
        if not sku:
            return ""
        barcodes = list(getattr(sku, "barcodes", []).all())
        size_value = (size or "").strip()
        if size_value:
            for barcode in barcodes:
                if (barcode.size or "").strip() == size_value:
                    return barcode.value or ""
        sku_code_barcode = str(getattr(sku, "code", "") or "").strip()
        if sku_code_barcode:
            return sku_code_barcode
        if not barcodes:
            return ""
        primary = next((barcode for barcode in barcodes if barcode.is_primary), None)
        if primary:
            return primary.value or ""
        return barcodes[0].value or ""

    @classmethod
    def _receiving_sku_map(cls, agency_id: int | None, items: list[dict]) -> dict[str, SKU]:
        if not agency_id or not items:
            return {}
        sku_codes = {
            str(item.get("sku_code") or item.get("sku") or "").strip()
            for item in items
            if str(item.get("sku_code") or item.get("sku") or "").strip()
        }
        if not sku_codes:
            return {}
        sku_filter = models.Q()
        for sku_code in sku_codes:
            sku_filter |= models.Q(sku_code__iexact=sku_code)
        result = {}
        for sku in (
            SKU.objects.filter(agency_id=agency_id, deleted=False)
            .filter(sku_filter)
            .prefetch_related("barcodes")
        ):
            key = str(sku.sku_code or "").strip().lower()
            if key and key not in result:
                result[key] = sku
        return result

    @classmethod
    def _receiving_barcode_rows(cls, sku: SKU) -> list[SKUBarcode]:
        cache = getattr(sku, "_prefetched_objects_cache", {})
        cached_rows = cache.get("barcodes")
        if cached_rows is not None:
            return list(cached_rows)
        return list(sku.barcodes.all())

    @staticmethod
    def _normalized_nomenclature_value(value) -> str:
        return str(value or "").strip()

    @classmethod
    def _barcode_for_size(cls, sku: SKU, size: str = "") -> SKUBarcode | None:
        size_key = cls._normalized_nomenclature_value(size).lower()
        if size_key:
            for barcode in cls._receiving_barcode_rows(sku):
                if cls._normalized_nomenclature_value(barcode.size).lower() == size_key:
                    return barcode
        primary = next((barcode for barcode in cls._receiving_barcode_rows(sku) if barcode.is_primary), None)
        if primary:
            return primary
        rows = cls._receiving_barcode_rows(sku)
        return rows[0] if rows else None

    @classmethod
    def _generate_unique_receiving_barcode(cls) -> str:
        for _ in range(100):
            timestamp_part = int(timezone.now().timestamp() * 1000) % 1_000_000_000
            random_part = random.randint(0, 99)
            candidate = f"29{timestamp_part:09d}{random_part:02d}"
            if not SKUBarcode.objects.filter(value=candidate).exists():
                return candidate
        raise ReceivingNomenclatureError("Не удалось сгенерировать уникальный штрихкод.")

    @classmethod
    def _barcode_value_for_new_size(
        cls,
        *,
        sku: SKU,
        size: str,
        requested_barcode: str = "",
    ) -> str:
        barcode_value = cls._normalized_nomenclature_value(requested_barcode)
        if barcode_value:
            existing = (
                SKUBarcode.objects.select_related("sku")
                .filter(agency_id=sku.agency_id, value=barcode_value)
                .first()
            )
            if existing and existing.sku_id != sku.id:
                raise ReceivingNomenclatureError(
                    f"ШК {barcode_value} уже используется для артикула {existing.sku.sku_code}."
                )
            if not existing:
                return barcode_value
        return cls._generate_unique_receiving_barcode()

    @classmethod
    def _update_receiving_sku_fields(
        cls,
        sku: SKU,
        *,
        name: str = "",
        brand: str = "",
        color: str = "",
        size: str = "",
        code: str = "",
    ) -> None:
        update_fields: list[str] = []
        normalized_name = cls._normalized_nomenclature_value(name)
        normalized_brand = cls._normalized_nomenclature_value(brand)
        normalized_color = cls._normalized_nomenclature_value(color)
        normalized_size = cls._normalized_nomenclature_value(size)
        normalized_code = cls._normalized_nomenclature_value(code)

        current_name = cls._normalized_nomenclature_value(sku.name)
        current_code = cls._normalized_nomenclature_value(sku.sku_code)
        if normalized_name and (not current_name or current_name == current_code):
            sku.name = normalized_name
            update_fields.append("name")
        if normalized_brand and not cls._normalized_nomenclature_value(sku.brand):
            sku.brand = normalized_brand
            update_fields.append("brand")
        if normalized_color and not cls._normalized_nomenclature_value(sku.color):
            sku.color = normalized_color
            update_fields.append("color")
        if normalized_size and not cls._normalized_nomenclature_value(sku.size):
            sku.size = normalized_size
            update_fields.append("size")
        if normalized_code and not cls._normalized_nomenclature_value(sku.code):
            sku.code = normalized_code
            update_fields.append("code")
        if update_fields:
            sku.save(update_fields=[*update_fields, "updated_at"])

    @classmethod
    def _create_receiving_sku_barcode(
        cls,
        *,
        sku: SKU,
        barcode_value: str,
        size: str = "",
    ) -> SKUBarcode:
        barcode = SKUBarcode.objects.create(
            sku=sku,
            value=barcode_value,
            size=cls._normalized_nomenclature_value(size) or None,
            is_primary=not sku.barcodes.exists(),
        )
        cache = getattr(sku, "_prefetched_objects_cache", None)
        if cache is not None and "barcodes" in cache:
            cache["barcodes"] = [*cache["barcodes"], barcode]
        return barcode

    @classmethod
    def _resolve_receiving_sku_for_item(
        cls,
        *,
        agency: Agency | None,
        item: dict,
        sku_by_id: dict[int, SKU],
        sku_by_code: dict[str, SKU],
    ) -> SKU:
        if not agency:
            raise ReceivingNomenclatureError("Не выбран клиент для сохранения номенклатуры.")

        sku_id_raw = item.get("sku_id")
        try:
            sku_id = int(str(sku_id_raw).strip()) if sku_id_raw not in (None, "") else None
        except (TypeError, ValueError):
            sku_id = None
        if sku_id is not None and sku_id in sku_by_id:
            return sku_by_id[sku_id]

        sku_code = cls._normalized_nomenclature_value(item.get("sku_code") or item.get("sku"))
        if sku_code:
            existing = sku_by_code.get(sku_code.lower())
            if existing:
                return existing

        label = sku_code or cls._normalized_nomenclature_value(item.get("barcode")) or "позиция"
        raise ReceivingNomenclatureError(
            f"Артикул {label} не найден в номенклатуре клиента. "
            "Приемка не создает SKU и штрихкоды автоматически: "
            "сначала создайте товар вручную или синхронизируйте маркетплейс."
        )

    @classmethod
    def _normalized_receiving_barcode(cls, value) -> str:
        normalized = cls._normalized_nomenclature_value(value)
        if normalized in {"-", "—", "–"}:
            return ""
        return normalized

    @classmethod
    def _reconcile_receiving_flow_nomenclature(
        cls,
        *,
        agency: Agency | None,
        flow_state: dict | None,
        goods_type: str,
    ) -> tuple[dict, bool]:
        """Hydrate empty/placeholder flow barcodes from existing client SKU data."""
        if not isinstance(flow_state, dict):
            return {}, False
        reconciled = deepcopy(flow_state)
        changed = False
        for container_key in ("boxes", "pallets"):
            containers = reconciled.get(container_key)
            if not isinstance(containers, list):
                continue
            for container in containers:
                if not isinstance(container, dict):
                    continue
                items = container.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if cls._normalized_receiving_barcode(item.get("barcode")):
                        continue
                    trial = deepcopy(item)
                    trial["barcode"] = ""
                    try:
                        cls._attach_receiving_nomenclature_metadata(
                            agency=agency,
                            items=[trial],
                            goods_type=goods_type,
                            validate_only=True,
                        )
                    except ReceivingNomenclatureError:
                        continue
                    if trial != item:
                        item.clear()
                        item.update(trial)
                        changed = True
        return reconciled, changed

    @classmethod
    def _attach_receiving_nomenclature_metadata(
        cls,
        *,
        agency: Agency | None,
        items: list[dict] | None,
        goods_type: str,
        order_id: str = "",
        order_type: str = "receiving",
        validate_only: bool = False,
    ) -> list[dict]:
        del goods_type, order_id, order_type
        if not items:
            return items or []

        agency_id = getattr(agency, "id", None)
        if not agency_id:
            return items or []

        sku_ids = set()
        sku_codes = set()
        requested_barcodes = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            sku_code = cls._normalized_nomenclature_value(item.get("sku_code") or item.get("sku"))
            if sku_code:
                sku_codes.add(sku_code)
            requested_barcode = cls._normalized_receiving_barcode(item.get("barcode"))
            if requested_barcode:
                requested_barcodes.add(requested_barcode)
            sku_id_raw = item.get("sku_id")
            if sku_id_raw in (None, ""):
                continue
            try:
                sku_ids.add(int(str(sku_id_raw).strip()))
            except (TypeError, ValueError):
                continue

        sku_queryset = SKU.objects.filter(agency_id=agency_id, deleted=False)
        sku_filter = models.Q()
        has_sku_filter = False
        if sku_ids:
            sku_filter |= models.Q(id__in=sku_ids)
            has_sku_filter = True
        for sku_code in sku_codes:
            sku_filter |= models.Q(sku_code__iexact=sku_code)
            has_sku_filter = True
        if has_sku_filter:
            sku_queryset = sku_queryset.filter(sku_filter)
        else:
            sku_queryset = sku_queryset.none()
        sku_queryset = sku_queryset.prefetch_related("barcodes")
        sku_by_id = {}
        sku_by_code = {}
        for sku in sku_queryset:
            sku_by_id[int(sku.id)] = sku
            sku_key = cls._normalized_nomenclature_value(sku.sku_code).lower()
            if sku_key:
                sku_by_code[sku_key] = sku

        barcode_by_value = {
            cls._normalized_nomenclature_value(barcode.value): barcode
            for barcode in SKUBarcode.objects.select_related("sku").filter(
                agency_id=agency_id,
                value__in=requested_barcodes,
            )
        } if requested_barcodes else {}
        barcode_by_sku_size = {}
        for sku in sku_by_id.values():
            for barcode in sku.barcodes.all():
                size_key = cls._normalized_nomenclature_value(barcode.size or sku.size).lower()
                barcode_by_sku_size[(int(sku.id), size_key)] = barcode
                value_key = cls._normalized_nomenclature_value(barcode.value)
                if value_key:
                    barcode_by_value.setdefault(value_key, barcode)

        for item in items:
            if not isinstance(item, dict):
                continue
            if not any(
                cls._normalized_nomenclature_value(item.get(field))
                for field in ("sku_code", "sku", "name", "brand", "color", "size", "barcode")
            ):
                continue
            sku = cls._resolve_receiving_sku_for_item(
                agency=agency,
                item=item,
                sku_by_id=sku_by_id,
                sku_by_code=sku_by_code,
            )
            requested_barcode = cls._normalized_receiving_barcode(item.get("barcode"))
            requested_size = cls._normalized_nomenclature_value(item.get("size"))
            barcode_row = barcode_by_value.get(requested_barcode) if requested_barcode else None
            if requested_barcode:
                if barcode_row is None:
                    raise ReceivingNomenclatureError(
                        f"ШК {requested_barcode} не найден в номенклатуре клиента. "
                        "Приемка не создает штрихкоды автоматически: "
                        "сначала добавьте ШК вручную или синхронизируйте маркетплейс."
                    )
                if barcode_row and barcode_row.sku_id != sku.id:
                    requested_sku_code = cls._normalized_nomenclature_value(
                        item.get("sku_code") or item.get("sku")
                    )
                    barcode_sku_code = cls._normalized_nomenclature_value(
                        barcode_row.sku.sku_code
                    )
                    same_logical_sku = bool(
                        barcode_row.sku.agency_id == agency_id
                        and not barcode_row.sku.deleted
                        and requested_sku_code
                        and requested_sku_code.casefold() == barcode_sku_code.casefold()
                    )
                    if same_logical_sku:
                        sku = barcode_row.sku
                        sku_by_id[int(sku.id)] = sku
                        sku_by_code[requested_sku_code.lower()] = sku
                    else:
                        raise ReceivingNomenclatureError(
                            f"ШК {requested_barcode} уже используется для артикула {barcode_row.sku.sku_code}."
                        )
            target_size = requested_size
            if not target_size and barcode_row and barcode_row.sku_id == sku.id:
                target_size = cls._normalized_nomenclature_value(barcode_row.size or sku.size)
            if not target_size:
                target_size = cls._normalized_nomenclature_value(sku.size)

            if not validate_only:
                cls._update_receiving_sku_fields(
                    sku,
                    name=item.get("name") or "",
                    brand=item.get("brand") or "",
                    color=item.get("color") or "",
                    size=target_size,
                )

            barcode_for_item = None
            if target_size:
                if barcode_row and cls._normalized_nomenclature_value(barcode_row.size).lower() == target_size.lower():
                    barcode_for_item = barcode_row
                else:
                    barcode_for_item = barcode_by_sku_size.get((int(sku.id), target_size.lower()))
                    if barcode_for_item is None:
                        barcode_for_item = cls._barcode_for_size(sku, target_size)
                if barcode_for_item is None or cls._normalized_nomenclature_value(barcode_for_item.size).lower() != target_size.lower():
                    raise ReceivingNomenclatureError(
                        f"Для артикула {sku.sku_code} не найден заранее созданный ШК "
                        f"для размера {target_size}. Приемка не создает штрихкоды автоматически: "
                        "сначала добавьте ШК вручную или синхронизируйте маркетплейс."
                    )
            else:
                barcode_for_item = barcode_row or cls._barcode_for_size(sku)
                if barcode_for_item is None:
                    raise ReceivingNomenclatureError(
                        f"Для артикула {sku.sku_code} не найден заранее созданный ШК. "
                        "Приемка не создает штрихкоды автоматически: "
                        "сначала добавьте ШК вручную или синхронизируйте маркетплейс."
                    )

            if validate_only and requested_barcode and requested_barcode != str(barcode_for_item.value or "").strip():
                raise ReceivingNomenclatureError(
                    f"ШК {requested_barcode} не соответствует размеру {target_size or 'без размера'} артикула {sku.sku_code}."
                )

            item["sku_id"] = int(sku.id or 0)
            item["sku_code"] = cls._normalized_nomenclature_value(sku.sku_code)
            item["sku"] = item["sku_code"]
            item["name"] = cls._normalized_nomenclature_value(item.get("name")) or cls._normalized_nomenclature_value(sku.name)
            item["brand"] = cls._normalized_nomenclature_value(item.get("brand")) or cls._normalized_nomenclature_value(sku.brand)
            item["color"] = cls._normalized_nomenclature_value(item.get("color")) or cls._normalized_nomenclature_value(sku.color)
            item["size"] = target_size or cls._normalized_nomenclature_value(item.get("size"))
            item["barcode"] = cls._normalized_nomenclature_value(
                getattr(barcode_for_item, "value", "") or cls._barcode_value_for_sku(sku, item.get("size"))
            )
            item["nomenclature_kind"] = "sku"
            item.pop("temporary_nomenclature_id", None)
            item.pop("temporary_nomenclature_code", None)
            if barcode_for_item is not None:
                barcode_value_key = cls._normalized_nomenclature_value(barcode_for_item.value)
                barcode_size_key = cls._normalized_nomenclature_value(barcode_for_item.size or target_size).lower()
                if barcode_value_key:
                    barcode_by_value[barcode_value_key] = barcode_for_item
                barcode_by_sku_size[(int(sku.id), barcode_size_key)] = barcode_for_item
        if not validate_only:
            cache.delete(f"receiving-flow-catalog:{agency_id}")
        return items

    @classmethod
    def _receiving_marked_items_map(cls, agency_id: int | None, items: list[dict]) -> dict[str, dict]:
        sku_map = cls._receiving_sku_map(agency_id, items)
        result = {}
        fallback_result = {}
        for item in items or []:
            key = cls._item_key(
                item.get("sku_code"),
                item.get("name"),
                item.get("size"),
                item.get("brand"),
                item.get("color"),
            )
            sku_code_key = str(item.get("sku_code") or item.get("sku") or "").strip().lower()
            sku = sku_map.get(sku_code_key)
            candidate = {
                "sku_code": str(item.get("sku_code") or item.get("sku") or "").strip(),
                "name": str(item.get("name") or "").strip(),
                "size": str(item.get("size") or "").strip(),
                "barcode": cls._normalized_receiving_barcode(item.get("barcode")) or cls._barcode_value_for_sku(sku, item.get("size")),
                "qty": cls._parse_qty_value(item.get("qty") or item.get("actual_qty")) or 0,
                "sku_id": sku.id if sku else None,
            }
            if sku and sku.honest_sign:
                result[key] = candidate
            else:
                fallback_result[key] = candidate
        return result or fallback_result

    @classmethod
    def _collect_receiving_marking_units(
        cls,
        order_id: str,
        valid_box_codes: set[str],
        box_to_pallet: dict[str, str],
        marked_items_map: dict[str, dict],
    ) -> tuple[list[dict], dict[str, int], bool]:
        units = []
        totals = {}
        has_orphan_units = False
        marked_by_pair = {
            (
                str(item.get("sku_code") or "").strip().lower(),
                str(item.get("size") or "").strip().lower(),
            ): (item_key, item)
            for item_key, item in marked_items_map.items()
        }
        queryset = (
            MarkingCode.objects.filter(order_type="receiving", order_id=order_id, used_at__isnull=False)
            .order_by("used_at", "created_at", "id")
        )
        for code in queryset:
            matched = marked_by_pair.get(
                (
                    str(code.sku_code or "").strip().lower(),
                    str(code.size or "").strip().lower(),
                )
            )
            if not matched:
                has_orphan_units = True
                continue
            matched_key, item = matched
            box_code = str(code.box_barcode or "").strip()
            if not box_code or box_code not in valid_box_codes:
                has_orphan_units = True
                continue
            totals[matched_key] = int(totals.get(matched_key, 0)) + 1
            units.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "",
                    "size": item.get("size") or "",
                    "barcode": code.barcode or item.get("barcode") or "",
                    "marking_code": code.code,
                    "box_code": box_code,
                    "pallet_code": box_to_pallet.get(box_code, ""),
                    "qty": 1,
                }
            )
        return units, totals, has_orphan_units

    @classmethod
    def _normalize_flow_items(cls, raw_items, default_goods_type: str = "") -> list[dict]:
        items = []
        fallback_goods_type = cls._normalize_flow_goods_type(default_goods_type)
        for raw in raw_items or []:
            if not isinstance(raw, dict):
                continue
            qty = cls._parse_qty_value(raw.get("qty")) or 0
            if qty <= 0:
                continue
            sku_code = (raw.get("sku_code") or raw.get("sku") or "").strip()
            barcode = cls._normalized_receiving_barcode(raw.get("barcode"))
            name = (raw.get("name") or "").strip()
            brand = (raw.get("brand") or "").strip()
            color = (raw.get("color") or "").strip()
            size = (raw.get("size") or "").strip()
            goods_type = cls._normalize_flow_goods_type(raw.get("goods_type"), fallback_goods_type)
            if not (sku_code or name or size):
                continue
            items.append(
                {
                    "sku_code": sku_code,
                    "sku": sku_code,
                    "barcode": barcode,
                    "name": name,
                    "brand": brand,
                    "color": color,
                    "size": size,
                    "goods_type": goods_type,
                    "qty": qty,
                }
            )
        return items

    @classmethod
    def _normalize_flow_goods_type(cls, value, fallback: str = "") -> str:
        raw = str(value or "").strip().lower()
        if raw:
            return raw
        fallback_value = str(fallback or "").strip().lower()
        return fallback_value

    @classmethod
    def _resolve_box_goods_type(cls, box: dict | None, items: list[dict] | None = None, default_goods_type: str = "") -> str:
        source_box = box if isinstance(box, dict) else {}
        explicit_goods_type = cls._normalize_flow_goods_type(source_box.get("goods_type"))
        if explicit_goods_type:
            return explicit_goods_type
        item_types: list[str] = []
        for item in items or source_box.get("items") or []:
            if not isinstance(item, dict):
                continue
            goods_type = cls._normalize_flow_goods_type(item.get("goods_type"))
            if goods_type and goods_type not in item_types:
                item_types.append(goods_type)
        if len(item_types) == 1:
            return item_types[0]
        return cls._normalize_flow_goods_type(default_goods_type)

    @classmethod
    def _box_uses_non_default_goods_type(cls, box: dict | None, default_goods_type: str = "") -> bool:
        normalized_default = cls._normalize_flow_goods_type(default_goods_type)
        if not normalized_default:
            return False
        source_box = box if isinstance(box, dict) else {}
        explicit_goods_type = cls._normalize_flow_goods_type(source_box.get("goods_type"))
        if explicit_goods_type and explicit_goods_type != normalized_default:
            return True
        for item in source_box.get("items") or []:
            if not isinstance(item, dict):
                continue
            goods_type = cls._normalize_flow_goods_type(item.get("goods_type"))
            if goods_type and goods_type != normalized_default:
                return True
        return False

    @classmethod
    def build_special_box_rows(
        cls,
        *,
        boxes,
        pallets,
        default_goods_type: str = "",
    ) -> list[dict]:
        normalized_default = cls._normalize_flow_goods_type(default_goods_type)
        if not normalized_default:
            return []

        box_to_pallet: dict[str, dict] = {}
        for pallet_index, pallet in enumerate(pallets or [], start=1):
            if not isinstance(pallet, dict):
                continue
            pallet_code = str(pallet.get("code") or "").strip()
            for box_index, box_code in enumerate(pallet.get("boxes") or [], start=1):
                normalized_box_code = str(box_code or "").strip()
                if not normalized_box_code:
                    continue
                box_to_pallet[normalized_box_code] = {
                    "pallet_code": pallet_code,
                    "pallet_index": pallet_index,
                    "box_index": box_index,
                }

        rows: list[dict] = []
        for box in boxes or []:
            if not isinstance(box, dict):
                continue
            if not cls._box_uses_non_default_goods_type(box, normalized_default):
                continue
            code = str(box.get("code") or "").strip()
            items = [item for item in (box.get("items") or []) if isinstance(item, dict)]
            if not code or not items:
                continue
            item_types: list[str] = []
            item_names: list[str] = []
            total_qty = 0
            for item in items:
                goods_type = cls._normalize_flow_goods_type(item.get("goods_type"))
                if goods_type and goods_type not in item_types:
                    item_types.append(goods_type)
                name = str(item.get("name") or item.get("sku_code") or "").strip()
                if name and name not in item_names:
                    item_names.append(name)
                total_qty += cls._parse_qty_value(item.get("qty")) or 0
            resolved_goods_type = cls._resolve_box_goods_type(box, items=items, default_goods_type=normalized_default)
            if len(item_types) > 1 and not cls._normalize_flow_goods_type(box.get("goods_type")):
                goods_type_label = "Смешанный"
            else:
                goods_type_label = cls.RECEIVING_GOODS_TYPE_LABELS.get(resolved_goods_type, resolved_goods_type or "-")
            preview = ", ".join(item_names[:3])
            if len(item_names) > 3:
                preview = f"{preview} +{len(item_names) - 3}"
            pallet_info = box_to_pallet.get(code, {})
            pallet_index = int(pallet_info.get("pallet_index") or 0)
            box_index = int(pallet_info.get("box_index") or 0)
            pallet_label = ""
            if pallet_index and box_index:
                pallet_label = f"Палета {pallet_index}, короб {box_index}"
            elif pallet_index:
                pallet_label = f"Палета {pallet_index}"
            rows.append(
                {
                    "code": code,
                    "goods_type": resolved_goods_type,
                    "goods_type_label": goods_type_label,
                    "qty": total_qty,
                    "items_label": preview or "-",
                    "pallet_code": str(pallet_info.get("pallet_code") or "").strip(),
                    "pallet_label": pallet_label,
                }
            )
        return rows

    @classmethod
    def build_special_item_rows(
        cls,
        *,
        boxes,
        default_goods_type: str = "",
    ) -> list[dict]:
        normalized_default = cls._normalize_flow_goods_type(default_goods_type)
        if not normalized_default:
            return []

        rows_by_key: dict[tuple[str, str], dict] = {}
        ordered_keys: list[tuple[str, str]] = []
        for box in boxes or []:
            if not isinstance(box, dict):
                continue
            items = [item for item in (box.get("items") or []) if isinstance(item, dict)]
            if not items:
                continue
            if not cls._box_uses_non_default_goods_type(box, normalized_default):
                continue
            resolved_box_type = cls._resolve_box_goods_type(
                box,
                items=items,
                default_goods_type=normalized_default,
            )
            counted_item_keys: set[tuple[str, str]] = set()
            for item in items:
                qty = cls._parse_qty_value(item.get("qty")) or 0
                if qty <= 0:
                    continue
                item_goods_type = cls._normalize_flow_goods_type(item.get("goods_type"), resolved_box_type)
                if not item_goods_type or item_goods_type == normalized_default:
                    continue
                sku_code = str(item.get("sku") or item.get("sku_code") or "").strip()
                name = str(item.get("name") or "").strip()
                brand = str(item.get("brand") or "").strip()
                color = str(item.get("color") or "").strip()
                size = str(item.get("size") or "").strip()
                item_key = cls._item_key(sku_code, name, size)
                group_key = (item_key, item_goods_type)
                row = rows_by_key.get(group_key)
                if row is None:
                    row = {
                        "item_key": item_key,
                        "goods_type": item_goods_type,
                        "goods_type_label": cls.RECEIVING_GOODS_TYPE_LABELS.get(item_goods_type, item_goods_type or "-"),
                        "default_goods_type": normalized_default,
                        "default_goods_type_label": cls.RECEIVING_GOODS_TYPE_LABELS.get(
                            normalized_default,
                            normalized_default or "-",
                        ),
                        "sku_code": sku_code,
                        "name": name or "-",
                        "brand": brand or "",
                        "color": color or "",
                        "size": size or "",
                        "qty": 0,
                        "box_qty": 0,
                    }
                    rows_by_key[group_key] = row
                    ordered_keys.append(group_key)
                row["qty"] += qty
                if group_key not in counted_item_keys:
                    row["box_qty"] += 1
                    counted_item_keys.add(group_key)
        return [rows_by_key[key] for key in ordered_keys]

    @classmethod
    def split_receiving_act_rows_by_goods_type(
        cls,
        rows: list[dict],
        *,
        special_rows: list[dict],
        planned_key: str,
        actual_key: str,
        box_key: str | None = None,
        sku_key: str = "sku_code",
        name_key: str = "name",
        size_key: str = "size",
        brand_key: str | None = None,
        color_key: str | None = None,
    ) -> list[dict]:
        if not rows or not special_rows:
            return [dict(row) for row in rows or [] if isinstance(row, dict)]

        grouped_special_rows: dict[str, list[dict]] = {}
        for special_row in special_rows:
            if not isinstance(special_row, dict):
                continue
            item_key = str(special_row.get("item_key") or "").strip()
            if not item_key:
                item_key = cls._item_key(
                    special_row.get(sku_key),
                    special_row.get(name_key),
                    special_row.get(size_key),
                    special_row.get(brand_key) if brand_key else None,
                    special_row.get(color_key) if color_key else None,
                )
            if not item_key:
                continue
            grouped_special_rows.setdefault(item_key, []).append(special_row)

        result: list[dict] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            row_copy = dict(row)
            item_key = cls._item_key(
                row_copy.get(sku_key),
                row_copy.get(name_key),
                row_copy.get(size_key),
                row_copy.get(brand_key) if brand_key else None,
                row_copy.get(color_key) if color_key else None,
            )
            item_special_rows = grouped_special_rows.get(item_key) or []
            if not item_special_rows:
                row_copy["is_special_goods_type_row"] = False
                result.append(row_copy)
                continue

            planned_total = cls._parse_qty_value(row_copy.get(planned_key)) or 0
            actual_total = cls._parse_qty_value(row_copy.get(actual_key)) or 0
            box_total = cls._parse_qty_value(row_copy.get(box_key)) if box_key else None
            actual_left = actual_total
            box_left = box_total
            split_rows: list[dict] = []
            default_goods_type = str(item_special_rows[0].get("default_goods_type") or "").strip()
            default_goods_type_label = str(
                item_special_rows[0].get("default_goods_type_label")
                or cls.RECEIVING_GOODS_TYPE_LABELS.get(default_goods_type, default_goods_type)
                or "-"
            ).strip()
            for special_row in item_special_rows:
                special_actual = cls._parse_qty_value(special_row.get("qty")) or 0
                special_box_qty = cls._parse_qty_value(special_row.get("box_qty")) or 0
                actual_left = max(actual_left - special_actual, 0)
                if box_left is not None:
                    box_left = max(box_left - special_box_qty, 0)
                special_copy = dict(row)
                goods_type_label = str(special_row.get("goods_type_label") or special_row.get("goods_type") or "-").strip()
                base_name = str(row.get(name_key) or "-").strip() or "-"
                special_copy[name_key] = f"{base_name} ({goods_type_label})"
                special_copy[actual_key] = special_actual
                if box_key is not None:
                    special_copy[box_key] = special_box_qty
                special_copy["goods_type"] = special_row.get("goods_type") or ""
                special_copy["goods_type_label"] = goods_type_label
                special_copy["is_special_goods_type_row"] = True
                special_copy["group_total_actual"] = actual_total
                split_rows.append(special_copy)

            row_copy[actual_key] = actual_left
            if box_key is not None and box_total is not None:
                row_copy[box_key] = box_left
            if default_goods_type:
                base_name = str(row.get(name_key) or "-").strip() or "-"
                row_copy[name_key] = f"{base_name} ({default_goods_type_label})"
                row_copy["goods_type"] = default_goods_type
                row_copy["goods_type_label"] = default_goods_type_label
            row_copy["is_special_goods_type_row"] = bool(default_goods_type)
            row_copy["group_total_actual"] = actual_total

            has_base_actual = bool(actual_left or (box_key is not None and (box_left or 0)))
            if has_base_actual or not split_rows:
                split_rows.insert(0, row_copy)

            primary_index = max(
                range(len(split_rows)),
                key=lambda index: cls._parse_qty_value(split_rows[index].get(actual_key)) or 0,
            )
            for index, split_row in enumerate(split_rows):
                split_row[planned_key] = planned_total if index == primary_index else 0
            result.extend(split_rows)
        return result

    @classmethod
    def normalize_receiving_flow_state(
        cls,
        *,
        boxes_data,
        pallets_data,
        active_box: str = "",
        active_pallet: str = "",
        default_goods_type: str = "",
    ) -> dict:
        cleaned_boxes = []
        seen_box_codes = set()
        for idx, box in enumerate(boxes_data or []):
            if not isinstance(box, dict):
                continue
            box_goods_type = cls._resolve_box_goods_type(box, default_goods_type=default_goods_type)
            items = cls._normalize_flow_items(box.get("items") or [], default_goods_type=box_goods_type)
            sealed = bool(box.get("sealed"))
            if not items and sealed:
                continue
            code = str(box.get("code") or "").strip() or f"BOX-{idx + 1}"
            if code in seen_box_codes:
                code = f"{code}-{idx + 1}"
            seen_box_codes.add(code)
            box_goods_type = cls._resolve_box_goods_type(box, items=items, default_goods_type=box_goods_type)
            cleaned_box = {
                "code": code,
                "items": items,
                "sealed": sealed,
                "goods_type": box_goods_type,
            }
            cleaned_box.update(WarehouseWritePathService.normalize_box_characteristics(box))
            cleaned_boxes.append(cleaned_box)

        cleaned_pallets = []
        seen_pallet_codes = set()
        for idx, pallet in enumerate(pallets_data or []):
            if not isinstance(pallet, dict):
                continue
            code = str(pallet.get("code") or "").strip() or f"PALLET-{idx + 1}"
            if code in seen_pallet_codes:
                code = f"{code}-{idx + 1}"
            sealed = bool(pallet.get("sealed"))
            boxes = [
                str(box_code).strip()
                for box_code in (pallet.get("boxes") or [])
                if str(box_code or "").strip()
            ]
            boxes = [box_code for box_code in boxes if box_code in seen_box_codes]
            pallet_goods_type = cls._resolve_box_goods_type(pallet, default_goods_type=default_goods_type)
            items = cls._normalize_flow_items(pallet.get("items") or [], default_goods_type=pallet_goods_type)
            pallet_goods_type = cls._resolve_box_goods_type(
                pallet,
                items=items,
                default_goods_type=pallet_goods_type,
            )
            if not boxes and not items and sealed:
                continue
            seen_pallet_codes.add(code)
            location = pallet.get("location")
            if isinstance(location, dict):
                location = dict(location)
            elif isinstance(location, str):
                location = {"zone": location}
            else:
                location = {}
            location.setdefault("zone", "PR")
            cleaned_pallets.append(
                {
                    "code": code,
                    "boxes": boxes,
                    "items": items,
                    "sealed": sealed,
                    "goods_type": pallet_goods_type,
                    "location": location,
                }
            )

        active_box_code = str(active_box or "").strip()
        if active_box_code not in seen_box_codes:
            open_boxes = [box for box in cleaned_boxes if not box.get("sealed")]
            if open_boxes:
                active_box_code = open_boxes[0].get("code") or ""
            elif cleaned_boxes:
                active_box_code = cleaned_boxes[0].get("code") or ""
            else:
                active_box_code = ""

        active_pallet_code = str(active_pallet or "").strip()
        if active_pallet_code not in seen_pallet_codes:
            open_pallets = [pallet for pallet in cleaned_pallets if not pallet.get("sealed")]
            if open_pallets:
                active_pallet_code = open_pallets[0].get("code") or ""
            elif cleaned_pallets:
                active_pallet_code = cleaned_pallets[0].get("code") or ""
            else:
                active_pallet_code = ""

        return {
            "boxes": cleaned_boxes,
            "pallets": cleaned_pallets,
            "activeBox": active_box_code,
            "activePallet": active_pallet_code,
        }

    @staticmethod
    def _normalize_eta_value(raw: str) -> str:
        eta_raw = (raw or "").strip()
        if not eta_raw:
            return ""
        try:
            return parse_business_datetime(eta_raw).isoformat()
        except ValueError:
            return eta_raw

    @staticmethod
    def _date_input_value(raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return ""
        try:
            return parse_business_datetime(text).date().isoformat()
        except ValueError:
            return ""

    @staticmethod
    def _parse_int_value(raw) -> int:
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _normalize_zone_code(cls, raw: str) -> str:
        text = (raw or "").strip().upper()
        return text or "PR"

    @classmethod
    def _normalize_receiving_move_location(cls, raw_location, fallback_payload=None) -> dict:
        source = raw_location if isinstance(raw_location, dict) else {}
        fallback = fallback_payload if isinstance(fallback_payload, dict) else {}
        zone = cls._normalize_zone_code(
            source.get("zone")
            or source.get("location")
            or fallback.get("zone")
            or fallback.get("location")
            or ""
        )
        row = cls._parse_int_value(source.get("row") or fallback.get("row")) if zone in {"MR", "OS"} else 0
        section = cls._parse_int_value(source.get("section") or fallback.get("section")) if zone == "OS" else 0
        tier = cls._parse_int_value(source.get("tier") or fallback.get("tier")) if zone == "OS" else 0
        cell = cls._parse_int_value(source.get("cell") or fallback.get("cell")) if zone == "OS" else 0
        return {
            "zone": zone,
            "row": row if zone in {"MR", "OS"} else "",
            "section": section if zone == "OS" else "",
            "tier": tier if zone == "OS" else "",
            "cell": cell if zone == "OS" else "",
        }

    @classmethod
    def _resolve_actor_name(cls, user) -> str:
        actor = cls._resolve_observer(user)
        if actor and actor.full_name:
            return actor.full_name
        auth_user = cls._authenticated_user(user)
        if auth_user:
            full_name = auth_user.get_full_name().strip()
            if full_name:
                return full_name
            username = getattr(auth_user, "username", "") or str(auth_user)
            if username:
                return username
        return "Сотрудник"

    @staticmethod
    def _shorten_ip_name(name: str) -> str:
        if not name:
            return "-"
        normalized = _IP_PREFIX_RE.sub("ИП", name)
        return " ".join(normalized.split()) or "-"

    @classmethod
    def _client_display(cls, agency: Agency | None) -> tuple[str, str]:
        if not agency:
            return "-", ""
        name = agency.short_name or agency.agn_name or agency.fio_agn or str(agency)
        return cls._shorten_ip_name(name), (agency.pref or "").strip()

    @classmethod
    def _resolve_observer(cls, user):
        actor = cls._authenticated_user(user)
        if not actor:
            return None
        return Employee.objects.filter(user=actor, is_active=True).first()

    @staticmethod
    def _flow_closed(entries) -> bool:
        for entry in reversed(entries or []):
            payload = entry.payload or {}
            if payload.get("flow_reopened"):
                return False
            if payload.get("flow_closed"):
                return True
            if (
                str(payload.get("act") or "").strip().lower() == "receiving"
                and str(payload.get("act_state") or "closed").strip().lower() == "closed"
            ):
                return True
        return False

    @staticmethod
    def _flow_reopened(entries) -> bool:
        for entry in reversed(entries or []):
            payload = entry.payload or {}
            if payload.get("flow_closed"):
                return False
            if payload.get("flow_reopened"):
                return True
        return False

    @classmethod
    def _pallet_placement_permissions_from_entries(cls, entries) -> dict[str, bool]:
        permissions: dict[str, bool] = {}
        for entry in entries or []:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if str(payload.get("event") or "").strip() != cls.PALLET_PLACEMENT_PERMISSION_EVENT:
                continue
            pallet_code = str(payload.get("pallet_code") or "").strip()
            if not pallet_code:
                continue
            permissions[pallet_code.casefold()] = bool(payload.get("pallet_placement_allowed"))
        return permissions

    @classmethod
    def _receiving_flow_pallet_stock_signature(
        cls,
        flow_state: dict | None,
        pallet_code: str,
    ) -> tuple | None:
        state = flow_state if isinstance(flow_state, dict) else {}
        normalized_code = str(pallet_code or "").strip().casefold()
        if not normalized_code:
            return None
        pallets = state.get("pallets") if isinstance(state.get("pallets"), list) else []
        pallet = next(
            (
                item
                for item in pallets
                if isinstance(item, dict)
                and str(item.get("code") or "").strip().casefold() == normalized_code
            ),
            None,
        )
        if not pallet:
            return None

        def item_signature(item: dict) -> tuple:
            source = item if isinstance(item, dict) else {}
            sku_identity = str(
                source.get("sku_code") or source.get("sku") or ""
            ).strip().casefold()
            if not sku_identity:
                sku_identity = str(source.get("barcode") or "").strip()
            if not sku_identity:
                sku_identity = "|".join(
                    (
                        str(source.get("name") or "").strip().casefold(),
                        str(source.get("size") or "").strip().casefold(),
                        str(source.get("brand") or "").strip().casefold(),
                        str(source.get("color") or "").strip().casefold(),
                    )
                )
            return (
                sku_identity,
                str(source.get("goods_type") or "").strip().casefold(),
                str(source.get("marking_code") or "").strip(),
                cls._parse_qty_value(source.get("qty")) or 0,
            )

        boxes = state.get("boxes") if isinstance(state.get("boxes"), list) else []
        boxes_by_code = {
            str(box.get("code") or "").strip().casefold(): box
            for box in boxes
            if isinstance(box, dict) and str(box.get("code") or "").strip()
        }
        pallet_box_codes = sorted(
            str(code or "").strip().casefold()
            for code in (pallet.get("boxes") or [])
            if str(code or "").strip()
        )
        box_signatures = []
        for box_code in pallet_box_codes:
            box = boxes_by_code.get(box_code)
            if not box:
                box_signatures.append((box_code, "", ()))
                continue
            box_signatures.append(
                (
                    box_code,
                    str(box.get("goods_type") or "").strip().casefold(),
                    tuple(sorted(item_signature(item) for item in (box.get("items") or []))),
                )
            )
        return (
            str(pallet.get("goods_type") or "").strip().casefold(),
            tuple(pallet_box_codes),
            tuple(sorted(item_signature(item) for item in (pallet.get("items") or []))),
            tuple(box_signatures),
        )

    @classmethod
    def receiving_flow_locked_pallet_changes(
        cls,
        *,
        order_id: str,
        previous_flow_state: dict | None,
        next_flow_state: dict | None,
    ) -> list[str]:
        previous_state = previous_flow_state if isinstance(previous_flow_state, dict) else {}
        previous_pallets = (
            previous_state.get("pallets")
            if isinstance(previous_state.get("pallets"), list)
            else []
        )
        statuses = cls.build_receiving_flow_pallet_takeout_statuses(order_id, previous_pallets)
        locked_statuses = {"ready", "moving", "placed"}
        changed_codes: list[str] = []
        for pallet in previous_pallets:
            if not isinstance(pallet, dict):
                continue
            pallet_code = str(pallet.get("code") or "").strip()
            if not pallet_code:
                continue
            status = str((statuses.get(pallet_code) or {}).get("status") or "").strip().lower()
            if status not in locked_statuses:
                continue
            previous_signature = cls._receiving_flow_pallet_stock_signature(
                previous_state,
                pallet_code,
            )
            next_signature = cls._receiving_flow_pallet_stock_signature(
                next_flow_state,
                pallet_code,
            )
            if previous_signature != next_signature:
                changed_codes.append(pallet_code)
        return changed_codes

    @classmethod
    def receiving_flow_pallet_placement_permissions(
        cls,
        *,
        order_id: str,
        entries=None,
    ) -> dict[str, bool]:
        source_entries = entries
        if source_entries is None:
            source_entries = OrderAuditEntry.objects.filter(
                order_type="receiving",
                order_id=str(order_id or "").strip(),
            ).only("payload").order_by("created_at", "id")
        return cls._pallet_placement_permissions_from_entries(source_entries)

    @classmethod
    def _validate_pallet_nomenclature_for_placement(cls, *, agency, flow_state, pallet):
        """Validate the saved pallet without changing catalog, stock or draft."""
        if not agency:
            raise ReceivingNomenclatureError("Не найден клиент приемки.")
        boxes = {str(box.get("code") or "").strip(): box for box in flow_state.get("boxes", []) if isinstance(box, dict)}
        containers = [pallet]
        for code in pallet.get("boxes") or []:
            box = boxes.get(str(code or "").strip())
            if not box or not box.get("sealed"):
                raise ReceivingNomenclatureError(f"Короб {code} не найден или не закрыт. Сохраните и закройте короб.")
            containers.append(box)
        all_items = []
        for container in containers:
            for item in container.get("items") or []:
                if not isinstance(item, dict) or (cls._parse_qty_value(item.get("qty")) or 0) <= 0:
                    continue
                barcode = str(item.get("barcode") or "").strip()
                if barcode in {"", "-", "—", "–"}:
                    label = item.get("sku_code") or item.get("sku") or item.get("name") or "позиция"
                    raise ReceivingNomenclatureError(
                        f"В {container.get('code') or 'палете'} у товара «{label}» не указан штрихкод. "
                        "Добавьте правильный ШК в номенклатуру клиента, укажите его в приемке и сохраните изменения."
                    )
                all_items.append(deepcopy(item))
        if not all_items:
            raise ReceivingNomenclatureError("В палете нет товаров. Проверьте и сохраните состав приемки.")
        cls._attach_receiving_nomenclature_metadata(
            agency=agency, items=all_items, goods_type=str(pallet.get("goods_type") or ""), validate_only=True,
        )

    @classmethod
    def allow_receiving_flow_pallet_placement(
        cls,
        *,
        order_id: str,
        pallet_code: str,
        receiving_location_code: str = "",
        fbs_cell_id=None,
        user=None,
        flow_client_version=None,
    ) -> dict:
        order_key = str(order_id or "").strip()
        code = str(pallet_code or "").strip()
        if not order_key or not code:
            return {"status": "missing_pallet", "reason": "missing_pallet"}

        with transaction.atomic():
            WarehouseCommandService.acquire_receiving_order_lock(order_key)
            entries = list(
                OrderAuditEntry.objects.select_for_update()
                .filter(order_type="receiving", order_id=order_key)
                .order_by("created_at", "id")
            )
            if not entries:
                return {"status": "missing_order", "reason": "missing_order"}
            flow_state = cls.find_receiving_flow_state(entries)
            pallets = flow_state.get("pallets") if isinstance(flow_state, dict) else []
            pallet = next(
                (
                    item
                    for item in pallets or []
                    if isinstance(item, dict)
                    and str(item.get("code") or "").strip().casefold() == code.casefold()
                ),
                None,
            )
            if not pallet:
                return {"status": "missing_pallet", "reason": "missing_pallet"}
            if not bool(pallet.get("sealed")):
                return {"status": "pallet_open", "reason": "pallet_open"}
            canonical_code = str(pallet.get("code") or code).strip()

            if flow_client_version is not None and (
                str(flow_client_version) != str(cls._latest_flow_client_version(entries))
            ):
                return {"status": "stale_draft", "message": "Приемка изменена в другой вкладке. Обновите страницу и проверьте состав палеты."}
            try:
                cls._validate_pallet_nomenclature_for_placement(
                    agency=entries[-1].agency, flow_state=flow_state, pallet=pallet,
                )
            except ReceivingNomenclatureError as exc:
                return {"status": "nomenclature_invalid", "message": "Размещение запрещено. " + str(exc), "pallet_code": str(pallet.get("code") or code)}
            mixed_pallets = cls.receiving_flow_mixed_pallet_goods_type_conflicts(
                flow_state=flow_state,
                default_goods_type=str(pallet.get("goods_type") or ""),
            )
            if canonical_code in mixed_pallets:
                return {
                    "status": "mixed_pallet_goods_types",
                    "message": (
                        "Размещение запрещено: готовый и необработанный товар "
                        "нельзя объединять на одной палете. Разделите палету по типам товара."
                    ),
                    "pallet_code": canonical_code,
                }

            permissions = cls._pallet_placement_permissions_from_entries(entries)
            current_status = cls.build_receiving_flow_pallet_takeout_statuses(
                order_key,
                [pallet],
            ).get(canonical_code, {})
            if str(current_status.get("status") or "").strip() == "placed":
                return {
                    "status": "already_placed",
                    "pallet_code": canonical_code,
                    "takeout_status": current_status,
                }
            if cls._flow_closed(entries):
                return {
                    "status": "already_allowed",
                    "pallet_code": canonical_code,
                    "takeout_status": {
                        "status": "ready",
                        "label": "Готова к забору",
                        "class": "ready-for-free-move",
                    },
                }

            if cls.receiving_route_from_entries(entries) == "fbs" and not fbs_cell_id:
                return {
                    "status": "fbs_destination_required",
                    "message": "Выберите конечное FBS-место для этой паллеты. Автоматический выбор отключён.",
                    "pallet_code": canonical_code,
                }
            normalized_receiving_location_code = str(receiving_location_code or "").strip()
            if cls.requires_concrete_receiving_location(entries):
                if not normalized_receiving_location_code:
                    return {
                        "status": "receiving_location_required",
                        "message": "Выберите конкретное свободное место приёмки перед размещением паллеты.",
                        "pallet_code": canonical_code,
                    }
                receiving_location = (
                    receiving_locations()
                    .filter(location_code__iexact=normalized_receiving_location_code)
                    .first()
                )
                if receiving_location is None and WarehouseLocation.objects.filter(
                    warehouse_code="MSK",
                    zone_code__iexact="PR",
                    location_code__iexact=normalized_receiving_location_code,
                    is_fbs_visible=True,
                ).exists():
                    return {
                        "status": "fbs_receiving_location",
                        "message": (
                            f"Место {normalized_receiving_location_code} относится к FBS. "
                            "Приёмка размещает товар только на места общего склада в зоне PR."
                        ),
                        "pallet_code": canonical_code,
                    }
                if receiving_location is None:
                    return {
                        "status": "invalid_receiving_location",
                        "message": "Выбранное место приёмки недоступно. Обновите страницу и выберите другое место.",
                        "pallet_code": canonical_code,
                    }
                occupied_location_ids = occupied_operational_location_ids(
                    warehouse_code="MSK",
                    zone_code="PR",
                    exclude_context_type="receiving",
                    exclude_context_id=order_key,
                )
                if receiving_location.id in occupied_location_ids:
                    return {
                        "status": "receiving_location_occupied",
                        "message": (
                            f"Место {receiving_location.location_code} уже занято. "
                            "Обновите страницу и выберите другое свободное место."
                        ),
                        "pallet_code": canonical_code,
                    }
                normalized_receiving_location_code = receiving_location.location_code
            if permissions.get(canonical_code.casefold(), False):
                # Разрешение выдано раньше, чем появилось оприходование при
                # разрешении: паллета так и осталась без остатка. Досоздаем его
                # здесь, пока приемка открыта — по закрытой это сделал акт.
                repeated = {}
                if canonical_code.casefold() not in cls.materialized_pallet_codes(
                    order_id=order_key,
                    entries=entries,
                ):
                    repeated = cls.materialize_receiving_flow_pallets(
                        order_id=order_key,
                        agency=entries[-1].agency,
                        flow_state=flow_state,
                        pallet_codes=[canonical_code],
                        receiving_location_code=normalized_receiving_location_code,
                        user=user,
                    )
                fbs_route = cls._auto_route_receiving_pallet_to_fbs(
                    order_id=order_key,
                    entries=entries,
                    pallet_code=canonical_code,
                    fbs_cell_id=fbs_cell_id,
                    user=user,
                )
                if fbs_route.get("status") == "blocked":
                    transaction.set_rollback(True)
                    return {"status": "fbs_destination_invalid", "message": fbs_route.get("message")}
                return {
                    "status": "already_allowed",
                    "pallet_code": canonical_code,
                    "materialized": repeated,
                    "fbs_route": fbs_route,
                    "takeout_status": {
                        "status": "ready",
                        "label": "Готова к забору",
                        "class": "ready-for-free-move",
                    },
                }

            actor = cls._authenticated_user(user)
            actor_name = cls._resolve_actor_name(user)
            latest = entries[-1]
            allowed_at = timezone.localtime().isoformat()
            OrderAuditEntry.objects.create(
                order_id=order_key,
                order_type="receiving",
                action="update",
                agency=latest.agency,
                user=actor,
                description=f"{actor_name} разрешил размещение паллеты {canonical_code}",
                payload={
                    "event": cls.PALLET_PLACEMENT_PERMISSION_EVENT,
                    "pallet_code": canonical_code,
                    "pallet_placement_allowed": True,
                    "pallet_placement_allowed_at": allowed_at,
                    "pallet_placement_allowed_by": actor_name,
                    "receiving_location_code": normalized_receiving_location_code,
                    "fbs_cell_id": fbs_cell_id,
                },
            )
            log_staff_overaction(
                "update",
                user=actor,
                agency=latest.agency,
                description=f"Разрешение размещения паллеты {canonical_code} (заявка {order_key})",
                snapshot={
                    "order_id": order_key,
                    "pallet_code": canonical_code,
                    "pallet_placement_allowed": True,
                    "pallet_placement_allowed_at": allowed_at,
                },
            )
            # Разрешенная паллета встает на остатки сразу и остается в зоне PR:
            # ричтрак больше не нужен, чтобы товар появился в системе, а FBS
            # забирает его прямо из приемки. Сбой оприходования обязан откатить
            # и само разрешение — иначе журнал утверждает то, чего нет в остатке.
            materialized = cls.materialize_receiving_flow_pallets(
                order_id=order_key,
                agency=latest.agency,
                flow_state=flow_state,
                pallet_codes=[canonical_code],
                receiving_location_code=normalized_receiving_location_code,
                user=user,
            )
            fbs_route = cls._auto_route_receiving_pallet_to_fbs(
                order_id=order_key,
                entries=entries,
                pallet_code=canonical_code,
                fbs_cell_id=fbs_cell_id,
                user=user,
            )
            if fbs_route.get("status") == "blocked":
                transaction.set_rollback(True)
                return {"status": "fbs_destination_invalid", "message": fbs_route.get("message")}
            return {
                "status": "allowed",
                "pallet_code": canonical_code,
                "materialized": materialized,
                "fbs_route": fbs_route,
                "takeout_status": {
                    "status": "ready",
                    "label": "Готова к забору",
                    "class": "ready-for-free-move",
                },
            }

    @classmethod
    def _auto_route_receiving_pallet_to_fbs(
        cls,
        *,
        order_id: str,
        entries,
        pallet_code: str,
        fbs_cell_id=None,
        user=None,
    ) -> dict:
        if cls.receiving_route_from_entries(entries) != "fbs":
            return {}
        latest = entries[-1] if entries else None
        if latest is None or not latest.agency_id:
            return {
                "status": "blocked",
                "message": "Не найден клиент для маршрута FBS.",
            }
        from fbs.exceptions import FbsInventoryError, FbsReplenishmentError, FbsStorageError
        from fbs.services.receiving_movements import auto_route_receiving_pallet_to_fbs

        try:
            return auto_route_receiving_pallet_to_fbs(
                agency_id=int(latest.agency_id),
                order_id=str(order_id or "").strip(),
                pallet_code=str(pallet_code or "").strip(),
                fbs_cell_id=fbs_cell_id,
                user=user,
            )
        except (FbsInventoryError, FbsReplenishmentError, FbsStorageError, ValueError) as exc:
            return {
                "status": "blocked",
                "message": str(exc),
            }

    @classmethod
    def receiving_flow_pallet_takeout_state(cls, *, order_id: str, pallet_code: str) -> dict:
        order_key = str(order_id or "").strip()
        code = str(pallet_code or "").strip()
        if not order_key or not code:
            return {"known": False, "can_take": True}
        entries = list(
            OrderAuditEntry.objects.filter(order_type="receiving", order_id=order_key).order_by("created_at", "id")
        )
        if not entries:
            return {"known": False, "can_take": True}
        flow_state = cls.find_receiving_flow_state(entries)
        pallets = flow_state.get("pallets") if isinstance(flow_state, dict) else []
        pallet = next(
            (
                item
                for item in pallets or []
                if isinstance(item, dict) and str(item.get("code") or "").strip().casefold() == code.casefold()
            ),
            None,
        )
        if not pallet:
            return {"known": False, "can_take": True}
        sealed = bool(pallet.get("sealed"))
        flow_closed = cls._flow_closed(entries)
        explicitly_allowed = cls._pallet_placement_permissions_from_entries(entries).get(code.casefold(), False)
        placement_allowed = bool(explicitly_allowed or flow_closed)
        if sealed and placement_allowed:
            return {
                "known": True,
                "can_take": True,
                "sealed": sealed,
                "flow_closed": flow_closed,
                "placement_allowed": True,
                "placement_permission_source": "flow_closed" if flow_closed else "explicit",
                "status": "ready",
            }
        if sealed:
            return {
                "known": True,
                "can_take": False,
                "sealed": True,
                "flow_closed": flow_closed,
                "placement_allowed": False,
                "status": "locked",
                "reason": f"Приемка {order_key}: размещение паллеты не разрешено кладовщиком.",
            }
        return {
            "known": True,
            "can_take": False,
            "sealed": False,
            "flow_closed": flow_closed,
            "placement_allowed": False,
            "status": "open",
            "reason": f"Приемка {order_key}: паллета еще открыта.",
        }

    @classmethod
    def materialized_pallet_codes(cls, *, order_id: str, entries=None) -> set[str]:
        """Паллеты приемки, по которым складской остаток уже создавался.

        Читается из журнала, а не из строк остатка: уехавший товар оставляет
        архивные строки с qty=0, и проверка по живым строкам пропустила бы
        повторное оприходование той же паллеты.
        """
        order_key = str(order_id or "").strip()
        if not order_key:
            return set()
        source_entries = entries
        if source_entries is None:
            source_entries = (
                OrderAuditEntry.objects.filter(order_type="receiving", order_id=order_key)
                .only("payload")
                .order_by("created_at", "id")
            )
        codes: set[str] = set()
        for entry in source_entries or []:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if str(payload.get("event") or "").strip() != cls.PALLET_STOCK_MATERIALIZED_EVENT:
                continue
            pallet_code = str(payload.get("pallet_code") or "").strip()
            if pallet_code:
                codes.add(pallet_code.casefold())
        return codes

    @classmethod
    def _record_pallet_stock_materialized(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        pallet_code: str,
        user=None,
    ) -> None:
        order_key = str(order_id or "").strip()
        code = str(pallet_code or "").strip()
        if not order_key or not code or not agency:
            return
        actor = cls._authenticated_user(user)
        actor_name = cls._resolve_actor_name(user)
        OrderAuditEntry.objects.create(
            order_id=order_key,
            order_type="receiving",
            action="update",
            agency=agency,
            user=actor,
            description=f"Паллета {code} поставлена на остатки в зоне приемки",
            payload={
                "event": cls.PALLET_STOCK_MATERIALIZED_EVENT,
                "pallet_code": code,
                "pallet_stock_materialized": True,
                "pallet_stock_materialized_at": timezone.localtime().isoformat(),
                "pallet_stock_materialized_by": actor_name,
            },
        )

    @classmethod
    def materialize_receiving_flow_pallets(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        flow_state: dict | None,
        pallet_codes: list[str] | tuple[str, ...] | set[str] | None = None,
        receiving_location_code: str = "",
        user=None,
    ) -> dict:
        order_key = str(order_id or "").strip()
        if not order_key or not agency or not isinstance(flow_state, dict):
            return {"created": 0, "skipped": 0, "missing": 0, "conflicts": 0}
        boxes = flow_state.get("boxes") if isinstance(flow_state.get("boxes"), list) else []
        pallets = flow_state.get("pallets") if isinstance(flow_state.get("pallets"), list) else []
        if not pallets:
            return {"created": 0, "skipped": 0, "missing": 0, "conflicts": 0}
        target_codes = {
            str(code or "").strip().casefold()
            for code in (pallet_codes or [])
            if str(code or "").strip()
        }
        boxes_by_code = {
            str(box.get("code") or "").strip(): box
            for box in boxes
            if isinstance(box, dict) and str(box.get("code") or "").strip()
        }
        created = 0
        skipped = 0
        missing = 0
        conflicts = 0
        actor = cls._authenticated_user(user)
        # Признак оприходования переживает уход товара, в отличие от строк остатка:
        # без него повторный заход (ричтрак, закрытие акта) создал бы остаток заново.
        materialized_codes = cls.materialized_pallet_codes(order_id=order_key)
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            pallet_code = str(pallet.get("code") or "").strip()
            if not pallet_code:
                continue
            if target_codes and pallet_code.casefold() not in target_codes:
                continue
            if pallet_code.casefold() in materialized_codes:
                skipped += 1
                continue
            if not bool(pallet.get("sealed")):
                skipped += 1
                continue
            pallet_box_codes = [
                str(box_code or "").strip()
                for box_code in (pallet.get("boxes") or [])
                if str(box_code or "").strip()
            ]
            pallet_boxes = [
                boxes_by_code[box_code]
                for box_code in pallet_box_codes
                if box_code in boxes_by_code
            ]
            placement_payload = {
                "act_boxes": pallet_boxes,
                "act_pallets": [pallet],
                "goods_type": str(pallet.get("goods_type") or "").strip(),
            }
            items = WarehouseWritePathService._receiving_items_from_placement_payload(
                order_id=order_key,
                placement_payload=placement_payload,
            )
            if items:
                cls._attach_receiving_nomenclature_metadata(
                    agency=agency,
                    items=items,
                    goods_type=str(pallet.get("goods_type") or "").strip(),
                    order_id=order_key,
                    order_type="receiving",
                )
            if not items:
                missing += 1
                continue
            existing_filter = (
                models.Q(parent_container__container_code__iexact=pallet_code)
                | models.Q(container__container_code__iexact=pallet_code)
                | models.Q(container_code__iexact=pallet_code)
            )
            if pallet_box_codes:
                existing_filter |= (
                    models.Q(container__container_code__in=pallet_box_codes)
                    | models.Q(container_code__in=pallet_box_codes)
                )
            existing_item_keys: set[tuple] = set()
            existing_container_keys: set[tuple[str, str]] = set()
            existing_snapshots_by_match_key: dict[tuple, list[WarehouseStockSnapshot]] = {}
            for snapshot in (
                WarehouseStockSnapshot.objects.select_related("container", "parent_container")
                .filter(
                    agency=agency,
                    source_context_type="receiving",
                    source_context_id=order_key,
                    is_archived=False,
                    qty__gt=0,
                )
                .filter(existing_filter)
                .order_by("id")
            ):
                existing_item_keys.add(WarehouseWritePathService._receiving_snapshot_identity_key(snapshot))
                existing_snapshots_by_match_key.setdefault(
                    WarehouseWritePathService._receiving_snapshot_match_key(snapshot),
                    [],
                ).append(snapshot)
                container_key = WarehouseWritePathService._receiving_snapshot_container_key(snapshot)
                if container_key[0] and container_key[1]:
                    existing_container_keys.add(container_key)
            if existing_item_keys or existing_container_keys:
                next_items = []
                pallet_conflicts = 0
                for item in items:
                    identity_key = WarehouseWritePathService._receiving_item_identity_key(item)
                    if identity_key in existing_item_keys:
                        continue
                    matching_snapshots = existing_snapshots_by_match_key.get(
                        WarehouseWritePathService._receiving_item_match_key(item),
                        [],
                    )
                    if matching_snapshots:
                        if len(matching_snapshots) == 1 and WarehouseWritePathService._sync_receiving_snapshot_qty(
                            matching_snapshots[0],
                            item,
                        ):
                            continue
                        pallet_conflicts += 1
                        continue
                    container_key = WarehouseWritePathService._receiving_item_container_key(item)
                    if container_key[0] and container_key[1] and container_key in existing_container_keys:
                        pallet_conflicts += 1
                        continue
                    next_items.append(item)
                conflicts += pallet_conflicts
                items = next_items
            if not items:
                skipped += 1
                continue
            WarehouseWritePathService.create_receiving_placement(
                agency=agency,
                order_id=order_key,
                items=items,
                performed_by=actor,
                source_document_type="receiving_flow_pallet",
                source_document_id=order_key,
                stock_context_type="receiving",
                warehouse_state_code=WarehouseStateCode.PLACED_IN_RECEIVING.value,
                receiving_location_code=receiving_location_code,
            )
            cls._record_pallet_stock_materialized(
                order_id=order_key,
                agency=agency,
                pallet_code=pallet_code,
                user=actor,
            )
            WarehouseWritePathService.sync_box_characteristics(
                agency=agency,
                order_id=order_key,
                order_type="receiving",
                boxes=pallet_boxes,
                performed_by=actor,
            )
            created += 1
        return {"created": created, "skipped": skipped, "missing": missing, "conflicts": conflicts}

    @classmethod
    def _resolve_storekeeper(cls, user):
        actor = cls._authenticated_user(user)
        if not actor:
            return None
        return Employee.objects.filter(
            user=actor,
            role="storekeeper",
            is_active=True,
        ).first()

    @classmethod
    def _receiving_work_owner(cls, entries) -> dict:
        status_entry = cls._current_status_entry(entries)
        payload = dict(status_entry.payload or {}) if status_entry else {}
        has_flow_items = cls.receiving_flow_has_items(cls.find_receiving_flow_state(entries))
        has_explicit_owner = bool(
            payload.get("storekeeper_employee_id")
            or str(payload.get("storekeeper_name") or "").strip()
        )
        if (
            not has_flow_items
            and not payload.get("receiving_tsd_claimed")
            and not has_explicit_owner
        ):
            return {}
        status_label = str(payload.get("status_label") or "").strip().lower()
        if "взята в работу" not in status_label:
            return {}
        try:
            employee_id = int(payload.get("storekeeper_employee_id") or 0)
        except (TypeError, ValueError):
            employee_id = 0
        employee_name = str(payload.get("storekeeper_name") or "").strip()
        if not employee_id and not employee_name:
            return {}
        return {
            "storekeeper_employee_id": employee_id,
            "storekeeper_name": employee_name,
        }

    @classmethod
    def receiving_work_access(cls, *, entries, user) -> ReceivingActionResult:
        pending = cls.warehouse_invalid_review_payload(entries)
        if pending:
            return ReceivingActionResult(
                status="warehouse_review_pending",
                payload=pending,
                reason="warehouse_invalid_review_pending",
            )
        owner = cls._receiving_work_owner(entries)
        if not owner:
            return ReceivingActionResult(status="allowed")
        employee = cls._resolve_storekeeper(user)
        if employee and owner.get("storekeeper_employee_id") == employee.id:
            return ReceivingActionResult(status="allowed")
        return ReceivingActionResult(
            status="assigned_to_other_storekeeper",
            payload=owner,
            reason="receiving_work_owned",
        )

    @staticmethod
    def _assign_receiving_task_to_storekeeper(
        order_id: str,
        employee: Employee,
        *,
        started_at=None,
    ) -> int:
        update_values = {
            "assigned_to": employee,
            "status": "in_progress",
            "updated_at": timezone.localtime(),
        }
        if started_at is not None:
            if timezone.is_naive(started_at):
                started_at = timezone.make_aware(started_at, timezone.get_current_timezone())
            update_values["due_date"] = timezone.localtime(started_at) + timedelta(hours=24)
        return int(
            Task.objects.filter(
                route=f"/orders/receiving/{order_id}/",
                assigned_to__role="storekeeper",
            )
            .exclude(status="done")
            .update(**update_values)
        )

    @staticmethod
    def _close_manager_tasks(order_id: str) -> int:
        return int(
            Task.objects.filter(
                route=f"/orders/receiving/{order_id}/",
                assigned_to__role="manager",
            )
            .exclude(status="done")
            .update(status="done")
        )

    @staticmethod
    def _close_storekeeper_tasks(order_id: str) -> int:
        return int(
            Task.objects.filter(
                route=f"/orders/receiving/{order_id}/",
                assigned_to__role="storekeeper",
            )
            .exclude(status="done")
            .update(status="done")
        )

    @staticmethod
    def _invalid_receiving_effect_summary(order_id: str) -> dict:
        order_key = str(order_id or "").strip()
        counts = {
            "stock_snapshots": WarehouseStockSnapshot.objects.filter(
                source_context_type="receiving",
                source_context_id=order_key,
            ).count(),
            "inventory_states": InventoryState.objects.filter(
                order_type="receiving",
                order_id=order_key,
            ).count(),
            "pallet_states": StockPalletState.objects.filter(
                order_type="receiving",
                order_id=order_key,
            ).count(),
            "containers": WarehouseContainer.objects.filter(
                source_context_type="receiving",
                source_context_id=order_key,
            ).count(),
            "events": WarehouseEvent.objects.filter(
                models.Q(stock_context_type="receiving", stock_context_id=order_key)
                | models.Q(source_document_type="receiving", source_document_id=order_key)
            ).count(),
            "operations": WarehouseOperation.objects.filter(
                models.Q(context_type="receiving", context_id=order_key)
                | models.Q(source_document_type="receiving", source_document_id=order_key)
            ).count(),
            "reserves": WarehouseReserve.objects.filter(
                models.Q(context_type="receiving", context_id=order_key)
                | models.Q(source_document_type="receiving", source_document_id=order_key)
            ).count(),
        }
        counts["has_effects"] = any(counts.values())
        return counts

    @staticmethod
    def warehouse_invalid_review_payload(entries) -> dict:
        for entry in reversed(entries or []):
            payload = dict(entry.payload or {})
            if entry.action == "warehouse_invalid_review_resolved":
                return {}
            if payload.get("warehouse_invalid_review_pending"):
                return payload
            status = str(payload.get("status") or payload.get("submit_action") or "").strip().lower()
            if status in {"cancelled", "canceled", "done", "completed", "closed"}:
                return {}
        return {}

    @classmethod
    def _ensure_invalid_receiving_review_task(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        user,
        reason_label: str,
        note: str,
        effects: dict,
    ) -> bool:
        reviewer = (
            Employee.objects.filter(role="head_manager", is_active=True)
            .order_by("full_name", "id")
            .first()
        )
        if not reviewer:
            return False
        route = f"/orders/receiving/{order_id}/"
        title = f"Разобрать ошибочную приемку №{order_id}"
        client_label = (
            agency.agn_name or agency.inn or str(agency.id)
            if agency
            else "Клиент не определен"
        )
        effect_labels = {
            "stock_snapshots": "остатки",
            "inventory_states": "состояния товара",
            "pallet_states": "паллетные позиции",
            "containers": "короба/паллеты",
            "events": "складские события",
            "operations": "операции",
            "reserves": "резервы",
        }
        effect_text = ", ".join(
            f"{effect_labels[key]}: {value}"
            for key, value in effects.items()
            if key in effect_labels and value
        )
        description = f"Клиент: {client_label}\nПричина: {reason_label}"
        if note:
            description += f"\nКомментарий кладовщика: {note}"
        if effect_text:
            description += f"\nОбнаружено в складском контуре: {effect_text}."
        description += "\nАвтоматические изменения остатков, зон, резервов и коробов не выполнялись."
        task = (
            Task.objects.select_for_update()
            .filter(route=route, title=title, assigned_to=reviewer)
            .exclude(status="done")
            .first()
        )
        if task:
            task.description = description
            task.priority = "high"
            task.due_date = timezone.localtime()
            task.save(update_fields=["description", "priority", "due_date", "updated_at"])
            return True
        Task.objects.create(
            title=title,
            description=description,
            route=route,
            assigned_to=reviewer,
            created_by=cls._authenticated_user(user),
            status="in_progress",
            priority="high",
            due_date=timezone.localtime(),
        )
        return True

    @classmethod
    @transaction.atomic
    def close_invalid_receiving_task(
        cls,
        *,
        order_id: str,
        role: str,
        user,
        reason_code: str,
        note: str = "",
    ) -> ReceivingActionResult:
        if role != "storekeeper":
            raise PermissionError("Закрыть складскую задачу может только кладовщик")
        reason_key = str(reason_code or "").strip().lower()
        reason_label = cls.INVALID_RECEIVING_REASON_LABELS.get(reason_key)
        if not reason_label:
            raise ValueError("Выберите причину закрытия задачи")
        employee = cls._resolve_storekeeper(user)
        if not employee:
            raise PermissionError("Активный профиль кладовщика не найден")

        order_key = str(order_id or "").strip()
        WarehouseCommandService.acquire_receiving_order_lock(order_key)
        route = f"/orders/receiving/{order_key}/"
        entries = list(
            OrderAuditEntry.objects.select_for_update()
            .filter(order_id=order_key, order_type="receiving")
            .order_by("created_at", "id")
        )
        if not entries:
            raise ValueError("Заявка на приемку не найдена")
        if cls.warehouse_invalid_review_payload(entries):
            return ReceivingActionResult(status="review_already_pending")

        active_tasks = Task.objects.select_for_update().filter(
            route=route,
            assigned_to__role="storekeeper",
        ).exclude(status="done")
        latest = entries[-1]
        payload = dict(cls._latest_payload(entries))
        current_status_entry = cls._current_status_entry(entries)
        if current_status_entry:
            payload.update(dict(current_status_entry.payload or {}))
        note_text = str(note or "").strip()[:1000]
        effects = cls._invalid_receiving_effect_summary(order_key)
        common_payload = {
            "warehouse_invalid_reason": reason_key,
            "warehouse_invalid_reason_label": reason_label,
            "warehouse_invalid_note": note_text,
            "warehouse_invalid_reported_by_id": employee.id,
            "warehouse_invalid_reported_by": employee.full_name,
            "warehouse_invalid_reported_at": timezone.localtime().isoformat(),
            "warehouse_invalid_effects": effects,
        }

        if effects["has_effects"]:
            if not cls._ensure_invalid_receiving_review_task(
                order_id=order_key,
                agency=latest.agency,
                user=user,
                reason_label=reason_label,
                note=note_text,
                effects=effects,
            ):
                raise ValueError("Не найден активный начальник склада для проверки заявки")
            payload.update(common_payload)
            payload["warehouse_invalid_review_pending"] = True
            log_order_action(
                "warehouse_invalid_review_requested",
                order_id=order_key,
                order_type="receiving",
                user=cls._authenticated_user(user),
                agency=latest.agency,
                description=f"Кладовщик закрыл задачу с передачей начальнику склада: {reason_label}",
                payload=payload,
            )
            active_tasks.update(status="done")
            return ReceivingActionResult(status="review_required", payload=payload, meta=effects)

        payload.update(common_payload)
        deleted_at = timezone.localtime().isoformat()
        previous_status = str(
            payload.get("status") or payload.get("submit_action") or ""
        ).strip()
        previous_status_label = str(payload.get("status_label") or "").strip()
        payload.update(
            {
                "status": "cancelled",
                "submit_action": "cancelled",
                "status_label": "Удалена как ошибочная",
                "cancel_reason": reason_label,
                "cancelled_by": "storekeeper",
                "cancelled_at": deleted_at,
                "deleted_as_erroneous": True,
                "deleted_reason": reason_label,
                "deleted_at": deleted_at,
                "deleted_by_role": "storekeeper",
                "deleted_by_employee_id": employee.id,
                "deleted_by_employee_name": employee.full_name,
                "deleted_previous_status": previous_status,
                "deleted_previous_status_label": previous_status_label,
            }
        )
        log_order_action(
            "warehouse_erroneous_deleted",
            order_id=order_key,
            order_type="receiving",
            user=cls._authenticated_user(user),
            agency=latest.agency,
            description=f"Кладовщик отметил заявку удалённой как ошибочную: {reason_label}",
            payload=payload,
        )
        Task.objects.filter(route=route).exclude(status="done").update(status="done")
        return ReceivingActionResult(status="closed", payload=payload, meta=effects)

    @staticmethod
    def warehouse_cancel_request_payload(entries) -> dict:
        for entry in reversed(entries or []):
            payload = dict(entry.payload or {})
            action = str(entry.action or "").strip().lower()
            status = str(payload.get("status") or "").strip().lower()
            if action == "warehouse_cancel_request" or status == "warehouse_cancel_requested":
                return payload
            if action in {"warehouse_cancel_rejected", "warehouse_cancel_approved"}:
                return {}
            if status in {"cancelled", "canceled", "done", "completed", "closed"}:
                return {}
        return {}

    @classmethod
    def requires_warehouse_cancel_confirmation(cls, entries) -> bool:
        state_result = cls._receiving_state_result(entries)
        warehouse_state_started = bool(
            state_result
            and state_result.code
            in {
                WarehouseStateCode.RECEIVED_UNPLACED,
            }
        )
        manager_handed_to_warehouse = any(
            str((entry.payload or {}).get("status") or "").strip().lower()
            in {"warehouse", "on_warehouse"}
            or "ожидании поставки" in str(
                (entry.payload or {}).get("status_label") or ""
            ).strip().lower()
            or str(entry.description or "").strip().lower()
            == "подтверждено и отправлено на склад"
            for entry in entries or []
        )
        return warehouse_state_started or manager_handed_to_warehouse

    @classmethod
    def receiving_cancel_is_final(cls, entries) -> bool:
        state_result = cls._receiving_state_result(entries)
        return bool(
            state_result
            and state_result.code
            in {
                WarehouseStateCode.PLACED_IN_RECEIVING,
                WarehouseStateCode.STORED,
            }
        )

    @classmethod
    def _ensure_warehouse_cancel_review_task(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        user=None,
        reason: str = "",
    ) -> bool:
        if not agency:
            return False
        reviewer = (
            Employee.objects.filter(role="storekeeper", is_active=True).order_by("full_name").first()
            or Employee.objects.filter(role="processing_head", is_active=True)
            .order_by("full_name")
            .first()
        )
        if not reviewer:
            return False
        route = f"/orders/receiving/{order_id}/"
        title = f"Подтвердите отмену приемки №{order_id}"
        description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
        if str(reason or "").strip():
            description += f"\nПричина отмены: {str(reason).strip()}"
        task = (
            Task.objects.filter(route=route, assigned_to=reviewer, title=title)
            .exclude(status="done")
            .first()
        )
        if task:
            task.description = description
            task.due_date = timezone.localtime()
            task.save(update_fields=["description", "due_date", "updated_at"])
            return True
        Task.objects.create(
            title=title,
            description=description,
            route=route,
            assigned_to=reviewer,
            created_by=cls._authenticated_user(user),
            due_date=timezone.localtime(),
        )
        return True

    @staticmethod
    def _close_warehouse_cancel_review_task(order_id: str) -> int:
        return int(
            Task.objects.filter(
                route=f"/orders/receiving/{order_id}/",
                title__startswith="Подтвердите отмену приемки",
            )
            .exclude(status="done")
            .update(status="done")
        )

    @classmethod
    def request_warehouse_cancel_confirmation(
        cls,
        *,
        order_id: str,
        entries,
        user=None,
        reason: str,
    ) -> None:
        if cls.receiving_cancel_is_final(entries):
            raise ValueError("Приёмка уже сформировала складской остаток и не может быть отменена")
        if not cls.requires_warehouse_cancel_confirmation(entries):
            raise ValueError("Подтверждение склада для этой заявки не требуется")
        cancel_reason = str(reason or "").strip()
        if not cancel_reason:
            raise ValueError("Укажите причину отмены")
        if cls.warehouse_cancel_request_payload(entries):
            return
        latest = entries[-1]
        current_payload = dict(cls._latest_payload(entries))
        request_payload = dict(current_payload)
        request_payload.update(
            {
                "status": "warehouse_cancel_requested",
                "status_label": "Отмена ожидает подтверждения склада",
                "cancel_reason": cancel_reason,
                "cancel_requested_by_manager": True,
                "warehouse_cancel_previous_status": current_payload.get(
                    "manager_cancel_previous_status"
                )
                or current_payload.get("status")
                or current_payload.get("submit_action")
                or "",
                "warehouse_cancel_previous_status_label": current_payload.get(
                    "manager_cancel_previous_status_label"
                )
                or current_payload.get("status_label")
                or "",
                "warehouse_cancel_previous_submit_action": current_payload.get(
                    "manager_cancel_previous_submit_action"
                )
                or current_payload.get("submit_action")
                or "",
            }
        )
        cls._ensure_warehouse_cancel_review_task(
            order_id=order_id,
            agency=latest.agency,
            user=user,
            reason=cancel_reason,
        )
        log_order_action(
            "warehouse_cancel_request",
            order_id=order_id,
            order_type="receiving",
            user=cls._authenticated_user(user),
            agency=latest.agency,
            description="Менеджер запросил подтверждение отмены у склада",
            payload=request_payload,
        )

    @classmethod
    def reject_warehouse_cancel_confirmation(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
    ) -> None:
        if role not in cls._WAREHOUSE_CANCEL_REVIEW_ROLES:
            raise PermissionError("Отклонить отмену может только склад")
        request_payload = cls.warehouse_cancel_request_payload(entries)
        if not request_payload:
            raise ValueError("Активный запрос на отмену не найден")
        restored = dict(request_payload)
        restored["status"] = restored.pop("warehouse_cancel_previous_status", "") or "warehouse"
        restored["status_label"] = restored.pop(
            "warehouse_cancel_previous_status_label", ""
        ) or "В ожидании поставки товара"
        restored["submit_action"] = restored.pop(
            "warehouse_cancel_previous_submit_action", ""
        ) or restored["status"]
        restored.pop("cancel_requested_by_manager", None)
        restored.pop("cancel_reason", None)
        restored.pop("cancel_requested_by_client", None)
        restored.pop("manager_cancel_previous_status", None)
        restored.pop("manager_cancel_previous_status_label", None)
        restored.pop("manager_cancel_previous_submit_action", None)
        cls._close_warehouse_cancel_review_task(order_id)
        latest = entries[-1]
        log_order_action(
            "warehouse_cancel_rejected",
            order_id=order_id,
            order_type="receiving",
            user=cls._authenticated_user(user),
            agency=latest.agency,
            description="Склад не подтвердил отмену; заявка остаётся в работе",
            payload=restored,
        )

    @classmethod
    def cancel_receiving_order(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
        reason: str,
        warehouse_approved: bool = False,
    ) -> None:
        if not entries:
            raise ValueError("Заявка не найдена")
        if cls.receiving_cancel_is_final(entries):
            raise PermissionError("Приёмка уже сформировала складской остаток и не может быть отменена")
        pending = cls.warehouse_cancel_request_payload(entries)
        needs_warehouse = cls.requires_warehouse_cancel_confirmation(entries)
        if (needs_warehouse or pending) and not warehouse_approved:
            raise PermissionError("После начала работы склада требуется его подтверждение")
        if warehouse_approved and role not in cls._WAREHOUSE_CANCEL_REVIEW_ROLES:
            raise PermissionError("Подтвердить отмену может только склад")
        if warehouse_approved and not pending:
            raise ValueError("Активный запрос на отмену не найден")
        cancel_reason = str(reason or "").strip()
        if not cancel_reason:
            raise ValueError("Укажите причину отмены")
        latest = entries[-1]
        payload = dict(cls._latest_payload(entries))
        payload.update(
            {
                "status": "cancelled",
                "submit_action": "cancelled",
                "status_label": "Отменена",
                "cancel_reason": cancel_reason,
                "cancelled_by": "warehouse" if warehouse_approved else "manager",
                "cancelled_at": timezone.localtime().isoformat(),
            }
        )
        for key in (
            "cancel_requested_by_manager",
            "cancel_requested_by_client",
            "warehouse_cancel_previous_status",
            "warehouse_cancel_previous_status_label",
            "warehouse_cancel_previous_submit_action",
            "manager_cancel_previous_status",
            "manager_cancel_previous_status_label",
            "manager_cancel_previous_submit_action",
        ):
            payload.pop(key, None)
        log_order_action(
            "warehouse_cancel_approved" if warehouse_approved else "status",
            order_id=order_id,
            order_type="receiving",
            user=cls._authenticated_user(user),
            agency=latest.agency,
            description=(
                f"Склад подтвердил отмену заявки. Причина: {cancel_reason}"
                if warehouse_approved
                else f"Заявка отменена менеджером. Причина: {cancel_reason}"
            ),
            payload=payload,
        )
        Task.objects.filter(route=f"/orders/receiving/{order_id}/").exclude(status="done").update(
            status="done"
        )

    @staticmethod
    def _current_status_entry(entries):
        for entry in reversed(entries or []):
            payload = entry.payload or {}
            has_status = bool(
                payload.get("status")
                or payload.get("status_label")
                or payload.get("submit_action")
            )
            # Placement snapshots are also written with action="status", but
            # they do not describe the receiving status. Treating one of
            # those snapshots as the current status drops goods_type and
            # receiving_mode when an old receiving flow is resumed.
            if has_status:
                return entry
        return entries[-1] if entries else None

    @classmethod
    def _receiving_has_started(cls, entries) -> bool:
        for entry in entries or []:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if (
                payload.get("storekeeper_employee_id")
                or str(payload.get("storekeeper_name") or "").strip()
                or payload.get("receiving_tsd_claimed")
                or isinstance(payload.get("flow_state"), dict)
            ):
                return True
        return False

    @classmethod
    def receiving_configuration_lock(cls, entries) -> dict:
        """Return the immutable receiving type/mode after work has started.

        New flows persist an explicit lock in the first configuration event.
        Legacy open flows are locked to their latest real receiving status so
        technical placement snapshots cannot erase or change the type.
        """
        for entry in reversed(entries or []):
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if not payload.get("receiving_configuration_locked"):
                continue
            goods_type = cls._normalize_flow_goods_type(
                payload.get("receiving_configuration_locked_goods_type")
                or payload.get("goods_type")
            )
            if goods_type not in cls.RECEIVING_GOODS_TYPE_LABELS:
                continue
            receiving_mode = cls._normalize_receiving_mode_for_goods_type(
                payload.get("receiving_configuration_locked_mode")
                or payload.get("receiving_mode")
                or "standard",
                goods_type,
            )
            return {
                "goods_type": goods_type,
                "receiving_mode": receiving_mode,
                "explicit": True,
            }

        if not cls._receiving_has_started(entries):
            return {}
        for entry in reversed(entries or []):
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            goods_type = cls._normalize_flow_goods_type(payload.get("goods_type"))
            if goods_type not in cls.RECEIVING_GOODS_TYPE_LABELS:
                continue
            receiving_mode = cls._normalize_receiving_mode_for_goods_type(
                payload.get("receiving_mode") or "standard",
                goods_type,
            )
            return {
                "goods_type": goods_type,
                "receiving_mode": receiving_mode,
                "explicit": False,
            }
        return {}

    @classmethod
    def receiving_flow_goods_type_conflicts(
        cls,
        *,
        previous_flow_state: dict | None,
        next_flow_state: dict | None,
        locked_goods_type: str,
    ) -> list[str]:
        """Reject a changed container type while preserving legacy containers."""
        locked_type = cls._normalize_flow_goods_type(locked_goods_type)
        if not locked_type:
            return []

        def container_types(flow_state):
            result = {}
            state = flow_state if isinstance(flow_state, dict) else {}
            for kind, containers in (
                ("box", state.get("boxes") or []),
                ("pallet", state.get("pallets") or []),
            ):
                for container in containers:
                    if not isinstance(container, dict):
                        continue
                    code = str(container.get("code") or "").strip()
                    if not code:
                        continue
                    container_type = cls._resolve_box_goods_type(
                        container,
                        default_goods_type=locked_type,
                    )
                    result[(kind, code)] = container_type or locked_type
            return result

        previous_types = container_types(previous_flow_state)
        next_types = container_types(next_flow_state)
        conflicts = []
        for key, next_type in next_types.items():
            expected_type = previous_types.get(key, locked_type)
            if next_type != expected_type:
                conflicts.append(key[1])
        return conflicts

    @classmethod
    def receiving_flow_mixed_pallet_goods_type_conflicts(
        cls,
        *,
        flow_state: dict | None,
        default_goods_type: str = "",
    ) -> list[str]:
        """Return pallets that combine containers/items of different goods types."""
        state = flow_state if isinstance(flow_state, dict) else {}
        normalized_default = cls._normalize_flow_goods_type(default_goods_type)
        boxes_by_code = {
            str(box.get("code") or "").strip().casefold(): box
            for box in (state.get("boxes") or [])
            if isinstance(box, dict) and str(box.get("code") or "").strip()
        }
        conflicts: list[str] = []
        for pallet in state.get("pallets") or []:
            if not isinstance(pallet, dict):
                continue
            pallet_code = str(pallet.get("code") or "").strip()
            if not pallet_code:
                continue
            pallet_type = cls._resolve_box_goods_type(
                pallet,
                default_goods_type=normalized_default,
            )
            content_types: set[str] = set()
            for item in pallet.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if (cls._parse_qty_value(item.get("qty")) or 0) <= 0:
                    continue
                item_type = cls._normalize_flow_goods_type(
                    item.get("goods_type"),
                    pallet_type or normalized_default,
                )
                if item_type:
                    content_types.add(item_type)
            for raw_box_code in pallet.get("boxes") or []:
                box = boxes_by_code.get(str(raw_box_code or "").strip().casefold())
                if not box:
                    continue
                box_type = cls._resolve_box_goods_type(
                    box,
                    default_goods_type=pallet_type or normalized_default,
                )
                item_types = {
                    cls._normalize_flow_goods_type(
                        item.get("goods_type"),
                        box_type or pallet_type or normalized_default,
                    )
                    for item in (box.get("items") or [])
                    if isinstance(item, dict)
                    and (cls._parse_qty_value(item.get("qty")) or 0) > 0
                }
                item_types.discard("")
                if item_types:
                    content_types.update(item_types)
                elif box_type:
                    content_types.add(box_type)
                if box_type and item_types and item_types != {box_type}:
                    content_types.add(box_type)
            if len(content_types) > 1 or (
                pallet_type and content_types and content_types != {pallet_type}
            ):
                conflicts.append(pallet_code)
        return conflicts

    @staticmethod
    def _warehouse_status_from_payload(payload: dict | None) -> bool:
        payload = payload or {}
        status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
        status_label = (payload.get("status_label") or "").lower()
        return status_value in {"warehouse", "on_warehouse"} or "склад" in status_label or "ожидании поставки" in status_label

    @classmethod
    def _receiving_state_result(cls, entries):
        if not entries:
            return None
        status_entry = cls._current_status_entry(entries)
        payload = cls._latest_payload(entries)
        return WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(entries[-1].order_id or ""),
            agency=entries[-1].agency if entries else None,
            payload=(status_entry.payload if status_entry else payload) or payload,
        )

    @classmethod
    def can_start_receiving_flow(cls, entries) -> bool:
        state_result = cls._receiving_state_result(entries)
        status_entry = cls._current_status_entry(entries) if entries else None
        status_payload = dict(status_entry.payload or {}) if status_entry else {}
        allowed = WarehouseActionPolicy.can_start_receiving_flow(
            state_result,
            flow_closed=cls._flow_closed(entries),
            allow_legacy_warehouse=bool(
                state_result
                and state_result.code == WarehouseStateCode.UNKNOWN
                and cls._warehouse_status_from_payload(status_payload)
            ),
        ).allowed
        if not allowed and cls._flow_reopened(entries):
            return True
        if not allowed and cls.can_resume_stored_receiving_draft(entries):
            return True
        return allowed

    @classmethod
    def can_resume_stored_receiving_draft(cls, entries) -> bool:
        if not entries or cls._flow_closed(entries):
            return False
        state_result = cls._receiving_state_result(entries)
        if not state_result or state_result.code != WarehouseStateCode.STORED:
            return False
        if cls._find_act_entry(entries, "receiving", "акт приемки"):
            return False
        flow_state = cls.find_receiving_flow_state(entries)
        return bool(flow_state.get("boxes") or flow_state.get("pallets"))

    @classmethod
    def can_create_receiving_act(cls, entries, *, role: str = "storekeeper") -> bool:
        if not entries:
            return False
        status_entry = cls._current_status_entry(entries)
        status_payload = status_entry.payload or {} if status_entry else {}
        status_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(getattr(status_entry, "order_id", "") or (entries[-1].order_id if entries else "")),
            agency=entries[-1].agency if entries else None,
            payload=status_payload,
        )
        return WarehouseActionPolicy.can_create_receiving_act(
            status_result,
            has_receiving_act=bool(cls._find_act_entry(entries, "receiving", "акт приемки")),
            role=role,
            allow_legacy_warehouse=bool(
                status_result.code == WarehouseStateCode.UNKNOWN
                and cls._warehouse_status_from_payload(status_payload)
            ),
        ).allowed

    @classmethod
    def can_autostart_receiving_act(cls, entries) -> bool:
        if not entries or cls._find_act_entry(entries, "receiving", "акт приемки"):
            return False
        status_entry = cls._current_status_entry(entries)
        return cls._warehouse_status_from_payload(status_entry.payload if status_entry else {})

    @classmethod
    def find_receiving_flow_state(cls, entries) -> dict:
        for entry in reversed(entries or []):
            payload = entry.payload or {}
            flow_state = payload.get("flow_state")
            if isinstance(flow_state, dict):
                return flow_state
            boxes = payload.get("flow_boxes")
            pallets = payload.get("flow_pallets")
            if boxes or pallets:
                return {
                    "boxes": boxes or [],
                    "pallets": pallets or [],
                    "activeBox": payload.get("flow_active_box") or "",
                    "activePallet": payload.get("flow_active_pallet") or "",
                }
        return {}

    @classmethod
    def receiving_flow_has_items(cls, flow_state: dict | None) -> bool:
        state = flow_state if isinstance(flow_state, dict) else {}
        containers = list(state.get("boxes") or []) + list(state.get("pallets") or [])
        for container in containers:
            if not isinstance(container, dict):
                continue
            for item in container.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if (cls._parse_qty_value(item.get("qty")) or 0) > 0:
                    return True
        return False

    @classmethod
    def _receiving_plan_key_resolver(cls, planned_items, *, agency_id=None):
        """Match receiving facts by article and a barcode owned by that article.

        Name, brand and color are descriptive and may be enriched after the
        request is submitted.  An exact planned barcode always wins.  Another
        active barcode of the same article/size is accepted only when the
        planned article/size points to one unambiguous row.
        """
        def descriptive_key(item):
            return cls._item_key(
                item.get("sku_code") or item.get("sku"), item.get("name"),
                item.get("size"), item.get("brand"), item.get("color"),
            )

        def exact_identity(item):
            barcode = str(item.get("barcode") or "").strip()
            sku = str(item.get("sku_code") or item.get("sku") or "").strip().casefold()
            size = str(item.get("size") or "").strip().casefold()
            return (barcode, sku, size) if barcode and sku else None

        def article_identity(item):
            sku = str(item.get("sku_code") or item.get("sku") or "").strip().casefold()
            size = str(item.get("size") or "").strip().casefold()
            return (sku, size) if sku else None

        canonical_by_exact_identity = {}
        planned_rows = []
        planned_skus = set()
        for item in planned_items:
            key = descriptive_key(item)
            stable_key = exact_identity(item)
            if stable_key is not None:
                canonical_by_exact_identity.setdefault(stable_key, key)
            article_key = article_identity(item)
            if article_key is not None:
                planned_skus.add(article_key[0])
            planned_rows.append((item, key, stable_key, article_key))

        keys_by_article = {}
        for _item, key, stable_key, article_key in planned_rows:
            if article_key is None:
                continue
            canonical_key = canonical_by_exact_identity.get(stable_key, key)
            keys_by_article.setdefault(article_key, set()).add(canonical_key)

        catalog_sizes_by_barcode_article = {}
        if agency_id and planned_skus:
            sku_filter = models.Q()
            for sku_code in planned_skus:
                sku_filter |= models.Q(sku__sku_code__iexact=sku_code)
            barcode_rows = (
                SKUBarcode.objects.filter(
                    sku__agency_id=agency_id,
                    sku__deleted=False,
                )
                .filter(sku_filter)
                .select_related("sku")
            )
            for barcode_row in barcode_rows:
                barcode = str(barcode_row.value or "").strip()
                sku = str(barcode_row.sku.sku_code or "").strip().casefold()
                if not barcode or sku not in planned_skus:
                    continue
                linked_size = str(
                    barcode_row.size or barcode_row.sku.size or ""
                ).strip().casefold()
                catalog_sizes_by_barcode_article.setdefault((barcode, sku), set()).add(linked_size)

        def barcode_belongs_to_article(stable_key):
            if stable_key is None:
                return False
            barcode, sku, size = stable_key
            linked_sizes = catalog_sizes_by_barcode_article.get((barcode, sku))
            if not linked_sizes:
                return False
            return not size or "" in linked_sizes or size in linked_sizes

        def resolve(item):
            key = descriptive_key(item)
            stable_key = exact_identity(item)
            canonical_key = canonical_by_exact_identity.get(stable_key)
            if canonical_key is not None:
                return canonical_key
            if stable_key is not None:
                candidates = keys_by_article.get(article_identity(item), set())
                if barcode_belongs_to_article(stable_key) and len(candidates) == 1:
                    return next(iter(candidates))
                return f"{key}|@barcode:{stable_key[0]}"
            return key

        return resolve

    @classmethod
    def prepare_receiving_flow_completion(
        cls,
        *,
        order_id: str,
        entries,
        boxes_raw: str,
        pallets_raw: str,
    ) -> ReceivingFlowPreparationResult:
        if not order_id or not entries:
            return ReceivingFlowPreparationResult(status="missing", reason="missing")
        status_entry = cls._current_status_entry(entries)
        status_payload = dict(status_entry.payload or {}) if status_entry else {}
        if cls._flow_reopened(entries):
            status_payload["flow_was_reopened"] = True
        payload = dict(cls._latest_payload(entries))
        for key in (
            "items",
            "receiving_mode",
            "eta_at",
            "vehicle_number",
            "goods_type",
            "goods_type_label",
        ):
            value = status_payload.get(key)
            if value not in (None, "", []):
                payload[key] = value
        planned_items = payload.get("items") or []
        receiving_configuration_lock = cls.receiving_configuration_lock(entries)
        default_goods_type = str(
            receiving_configuration_lock.get("goods_type")
            or payload.get("goods_type")
            or ""
        ).strip()
        is_shipping_return = default_goods_type.lower() == SHIPPING_RETURN_GOODS_TYPE
        receiving_mode = str(
            receiving_configuration_lock.get("receiving_mode")
            or cls.effective_receiving_mode(payload)
        ).strip().lower()
        receiving_agency_id = entries[-1].agency_id if entries else None
        marked_items_map = cls._receiving_marked_items_map(receiving_agency_id, planned_items)
        receiving_plan_key = cls._receiving_plan_key_resolver(
            planned_items,
            agency_id=receiving_agency_id,
        )
        plan_map = {}
        for item in planned_items:
            key = receiving_plan_key(item)
            planned_qty = cls._parse_qty_value(item.get("qty")) or 0
            entry = plan_map.setdefault(
                key,
                {
                    "sku_code": item.get("sku_code"),
                    "name": item.get("name"),
                    "brand": item.get("brand"),
                    "color": item.get("color"),
                    "size": item.get("size"),
                    "barcode": item.get("barcode"),
                    "goods_type": item.get("goods_type") or default_goods_type,
                    "source_goods_type": item.get("source_goods_type") or "",
                    "planned_qty": 0,
                    "comment": item.get("comment"),
                },
            )
            entry["planned_qty"] += planned_qty

        try:
            boxes_data = json.loads(boxes_raw or "[]")
            pallets_data = json.loads(pallets_raw or "[]")
        except json.JSONDecodeError:
            return ReceivingFlowPreparationResult(status="invalid", reason="invalid_json")
        if not isinstance(boxes_data, list):
            boxes_data = []
        if not isinstance(pallets_data, list):
            pallets_data = []

        normalized_next_flow_state = cls.normalize_receiving_flow_state(
            boxes_data=boxes_data,
            pallets_data=pallets_data,
            default_goods_type=default_goods_type,
        )
        if cls.receiving_flow_mixed_pallet_goods_type_conflicts(
            flow_state=normalized_next_flow_state,
            default_goods_type=default_goods_type,
        ):
            return ReceivingFlowPreparationResult(
                status="invalid",
                reason="receiving_mixed_pallet_goods_types",
            )

        if receiving_configuration_lock:
            previous_flow_state = cls.find_receiving_flow_state(entries)
            normalized_previous_flow_state = cls.normalize_receiving_flow_state(
                boxes_data=previous_flow_state.get("boxes") or [],
                pallets_data=previous_flow_state.get("pallets") or [],
                active_box=previous_flow_state.get("activeBox") or "",
                active_pallet=previous_flow_state.get("activePallet") or "",
                default_goods_type=default_goods_type,
            )
            if cls.receiving_flow_goods_type_conflicts(
                previous_flow_state=normalized_previous_flow_state,
                next_flow_state=normalized_next_flow_state,
                locked_goods_type=default_goods_type,
            ):
                return ReceivingFlowPreparationResult(
                    status="invalid",
                    reason="receiving_goods_type_locked",
                )

        cleaned_boxes = []
        seen_box_codes = set()
        for idx, box in enumerate(boxes_data):
            if not isinstance(box, dict):
                continue
            box_goods_type = cls._resolve_box_goods_type(box, default_goods_type=default_goods_type)
            items = cls._normalize_flow_items(box.get("items") or [], default_goods_type=box_goods_type)
            if not items:
                continue
            code = str(box.get("code") or "").strip() or f"BOX-{idx + 1}"
            if code in seen_box_codes:
                code = f"{code}-{idx + 1}"
            seen_box_codes.add(code)
            box_goods_type = cls._resolve_box_goods_type(box, items=items, default_goods_type=box_goods_type)
            cleaned_box = {
                "code": code,
                "items": items,
                "sealed": True,
                "goods_type": box_goods_type,
            }
            cleaned_box.update(WarehouseWritePathService.normalize_box_characteristics(box))
            cleaned_boxes.append(cleaned_box)

        cleaned_pallets = []
        seen_pallet_codes = set()
        for idx, pallet in enumerate(pallets_data):
            if not isinstance(pallet, dict):
                continue
            code = str(pallet.get("code") or "").strip() or f"PALLET-{idx + 1}"
            if code in seen_pallet_codes:
                code = f"{code}-{idx + 1}"
            seen_pallet_codes.add(code)
            boxes = [
                str(box_code).strip()
                for box_code in (pallet.get("boxes") or [])
                if str(box_code or "").strip()
            ]
            boxes = [box_code for box_code in boxes if box_code in seen_box_codes]
            pallet_goods_type = cls._resolve_box_goods_type(pallet, default_goods_type=default_goods_type)
            items = cls._normalize_flow_items(pallet.get("items") or [], default_goods_type=pallet_goods_type)
            pallet_goods_type = cls._resolve_box_goods_type(
                pallet,
                items=items,
                default_goods_type=pallet_goods_type,
            )
            if not boxes and not items:
                continue
            location = pallet.get("location")
            if isinstance(location, dict):
                location = dict(location)
            elif isinstance(location, str):
                location = {"zone": location}
            else:
                location = {}
            location.setdefault("zone", "PR")
            cleaned_pallets.append(
                {
                    "code": code,
                    "boxes": boxes,
                    "items": items,
                    "sealed": True,
                    "goods_type": pallet_goods_type,
                    "location": location,
                }
            )

        if not cleaned_boxes or not cleaned_pallets:
            return ReceivingFlowPreparationResult(status="invalid", reason="missing_containers")

        pallet_box_codes = set()
        for pallet in cleaned_pallets:
            for box_code in pallet.get("boxes") or []:
                if box_code:
                    pallet_box_codes.add(box_code)
        unassigned_boxes = [box for box in cleaned_boxes if box["code"] not in pallet_box_codes]
        if unassigned_boxes:
            return ReceivingFlowPreparationResult(status="invalid", reason="unassigned_boxes")

        totals = {}
        def add_total(item, qty, field):
            key = receiving_plan_key(item)
            entry = totals.setdefault(
                key,
                {
                    "box": 0,
                    "pallet": 0,
                    "total": 0,
                    "item": {
                        "sku_code": item.get("sku_code") or item.get("sku"),
                        "name": item.get("name"),
                        "brand": item.get("brand"),
                        "color": item.get("color"),
                        "size": item.get("size"),
                        "barcode": item.get("barcode"),
                        "goods_type": item.get("goods_type") or default_goods_type,
                    },
                },
            )
            entry[field] += qty
            entry["total"] += qty

        for box in cleaned_boxes:
            for item in box.get("items") or []:
                qty = cls._parse_qty_value(item.get("qty")) or 0
                add_total(item, qty, "box")
        for pallet in cleaned_pallets:
            for item in pallet.get("items") or []:
                qty = cls._parse_qty_value(item.get("qty")) or 0
                add_total(item, qty, "pallet")

        valid_box_codes = {box["code"] for box in cleaned_boxes}
        box_to_pallet = {}
        for pallet in cleaned_pallets:
            for box_code in pallet.get("boxes") or []:
                box_to_pallet[str(box_code or "").strip()] = pallet.get("code") or ""

        act_items = []
        has_mismatch = False
        for key, plan in plan_map.items():
            entry = totals.get(key, {"box": 0, "pallet": 0, "total": 0})
            actual_qty = entry["total"]
            if not is_shipping_return and actual_qty != plan["planned_qty"]:
                has_mismatch = True
            act_items.append(
                {
                    "sku_code": plan.get("sku_code"),
                    "name": plan.get("name"),
                    "brand": plan.get("brand"),
                    "color": plan.get("color"),
                    "size": plan.get("size"),
                    "barcode": plan.get("barcode"),
                    "goods_type": plan.get("goods_type") or default_goods_type,
                    "source_goods_type": plan.get("source_goods_type") or "",
                    "planned_qty": plan.get("planned_qty"),
                    "actual_qty": actual_qty,
                    "comment": plan.get("comment"),
                }
            )

        extra_keys = set(totals.keys()) - set(plan_map.keys())
        for key in extra_keys:
            entry = totals.get(key)
            if not entry or entry["total"] <= 0:
                continue
            source_item = entry.get("item") or {}
            if not is_shipping_return:
                has_mismatch = True
            act_items.append(
                {
                    "sku_code": source_item.get("sku_code") or "",
                    "name": source_item.get("name") or "",
                    "brand": source_item.get("brand") or "",
                    "color": source_item.get("color") or "",
                    "size": source_item.get("size") or "",
                    "barcode": source_item.get("barcode") or "",
                    "goods_type": source_item.get("goods_type") or default_goods_type,
                    "planned_qty": 0,
                    "actual_qty": entry["total"],
                    "comment": "",
                    "extra": True,
                }
            )

        if is_shipping_return:
            source_order = resolve_shipping_return_order(
                agency=entries[-1].agency if entries else None,
                number=status_payload.get("shipping_return_order_number"),
            )
            if source_order is None:
                return ReceivingFlowPreparationResult(
                    status="invalid",
                    reason="shipping_return_source_not_found",
                )
            valid_return, return_reason, _return_details = validate_shipping_return_items(
                source_order,
                act_items,
                exclude_receiving_order_id=order_id,
            )
            if not valid_return:
                return ReceivingFlowPreparationResult(status="invalid", reason=return_reason)
            actual_by_source_key = {}
            for item in act_items:
                source_key = shipping_return_item_key(
                    item.get("sku_code") or item.get("sku"),
                    item.get("size"),
                )
                actual_by_source_key[source_key] = (
                    actual_by_source_key.get(source_key, 0)
                    + int(item.get("actual_qty") or 0)
                )
            canonical_act_items = []
            seen_source_keys = set()
            for item in planned_items:
                source_key = shipping_return_item_key(
                    item.get("sku_code") or item.get("sku"),
                    item.get("size"),
                )
                if not source_key[0] or source_key in seen_source_keys:
                    continue
                seen_source_keys.add(source_key)
                canonical_act_items.append(
                    {
                        "sku_code": item.get("sku_code") or item.get("sku"),
                        "name": item.get("name") or "",
                        "brand": item.get("brand") or "",
                        "color": item.get("color") or "",
                        "size": item.get("size") or "",
                        "barcode": item.get("barcode") or "",
                        "goods_type": SHIPPING_RETURN_GOODS_TYPE,
                        "source_goods_type": item.get("source_goods_type") or "",
                        "planned_qty": int(item.get("qty") or 0),
                        "actual_qty": int(actual_by_source_key.get(source_key, 0)),
                        "comment": item.get("comment") or "",
                    }
                )
            act_items = canonical_act_items

        act_units = []
        if receiving_mode == "cz" and marked_items_map:
            act_units, marked_totals, has_orphan_units = cls._collect_receiving_marking_units(
                order_id,
                valid_box_codes,
                box_to_pallet,
                marked_items_map,
            )
            if has_orphan_units:
                return ReceivingFlowPreparationResult(status="invalid", reason="orphan_units")
            for key in marked_items_map:
                actual_total = int((totals.get(key) or {}).get("total") or 0)
                scanned_total = int(marked_totals.get(key, 0))
                if actual_total != scanned_total:
                    return ReceivingFlowPreparationResult(status="invalid", reason="marking_mismatch")

        if not act_items:
            return ReceivingFlowPreparationResult(status="invalid", reason="missing_items")

        flow_state = cls.normalize_receiving_flow_state(
            boxes_data=boxes_data,
            pallets_data=pallets_data,
            active_box="",
            active_pallet="",
            default_goods_type=default_goods_type,
        )
        has_closed_act = any(
            (entry.payload or {}).get("act") == "placement"
            and ((entry.payload or {}).get("act_state") or "closed") == "closed"
            for entry in entries
        )
        placement_items = []
        shipping_return_totals = {}
        if is_shipping_return:
            for container_kind, containers in (("box", cleaned_boxes), ("pallet", cleaned_pallets)):
                for container in containers:
                    for container_item in container.get("items") or []:
                        source_key = shipping_return_item_key(
                            container_item.get("sku_code") or container_item.get("sku"),
                            container_item.get("size"),
                        )
                        source_total = shipping_return_totals.setdefault(
                            source_key,
                            {"box": 0, "pallet": 0, "total": 0},
                        )
                        item_qty = cls._parse_qty_value(container_item.get("qty")) or 0
                        source_total[container_kind] += item_qty
                        source_total["total"] += item_qty
        for item in act_items:
            key = cls._item_key(
                item.get("sku_code"),
                item.get("name"),
                item.get("size"),
                item.get("brand"),
                item.get("color"),
            )
            if is_shipping_return:
                entry = shipping_return_totals.get(
                    shipping_return_item_key(item.get("sku_code"), item.get("size")),
                    {"box": 0, "pallet": 0, "total": 0},
                )
            else:
                entry = totals.get(key, {"box": 0, "pallet": 0, "total": 0})
            placement_items.append(
                {
                    "sku_code": item.get("sku_code"),
                    "name": item.get("name"),
                    "brand": item.get("brand"),
                    "color": item.get("color"),
                    "size": item.get("size"),
                    "barcode": item.get("barcode"),
                    "goods_type": item.get("goods_type") or default_goods_type,
                    "source_goods_type": item.get("source_goods_type") or "",
                    "actual_qty": item.get("actual_qty") or 0,
                    "box_qty": entry["box"],
                    "pallet_qty": entry["pallet"],
                    "comment": item.get("comment"),
                }
            )

        return ReceivingFlowPreparationResult(
            status="ok",
            status_payload=status_payload,
            receiving_mode=receiving_mode,
            boxes=cleaned_boxes,
            pallets=cleaned_pallets,
            act_items=act_items,
            placement_items=placement_items,
            act_units=act_units,
            flow_state=flow_state,
            has_mismatch=has_mismatch,
            normalized_eta=cls._normalize_eta_value(payload.get("eta_at") or ""),
            vehicle_number=(payload.get("vehicle_number") or "").strip(),
            has_closed_placement_act=has_closed_act,
        )

    @classmethod
    @transaction.atomic
    def save_receiving_flow_draft(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        boxes_raw: str,
        pallets_raw: str,
        active_box: str = "",
        active_pallet: str = "",
        received_at: str = "",
        draft_version=None,
        draft_base_version=None,
        user=None,
    ) -> ReceivingActionResult:
        if role != "storekeeper":
            return ReceivingActionResult(status="forbidden")
        order_key = str(order_id or "").strip()
        if not order_key:
            return ReceivingActionResult(status="missing")
        WarehouseCommandService.acquire_receiving_order_lock(order_key)
        entry_list = list(
            OrderAuditEntry.objects.filter(
                order_id=order_key,
                order_type="receiving",
            ).order_by("created_at", "id")
        )
        if not entry_list:
            return ReceivingActionResult(status="missing")
        entries = entry_list
        access = cls.receiving_work_access(entries=entries, user=user)
        if access.status != "allowed":
            return access
        flow_closed = cls._flow_closed(entries)
        if flow_closed:
            return ReceivingActionResult(status="closed", meta={"closed_noop": True})
        has_existing_flow_state = any(
            entry.action == "update" and bool((entry.payload or {}).get("flow_state"))
            for entry in (entries or [])
        )
        state_result = cls._receiving_state_result(entries)
        status_entry = cls._current_status_entry(entries)
        status_payload = dict(status_entry.payload or {}) if status_entry else {}
        can_start = WarehouseActionPolicy.can_start_receiving_flow(
            state_result,
            flow_closed=flow_closed,
            allow_legacy_warehouse=bool(
                state_result
                and state_result.code == WarehouseStateCode.UNKNOWN
                and cls._warehouse_status_from_payload(status_payload)
            ),
        ).allowed
        if not can_start and cls._flow_reopened(entries):
            can_start = True
        if not can_start and has_existing_flow_state:
            can_start = True
        if not can_start:
            return ReceivingActionResult(status="not_allowed")
        try:
            boxes_data = json.loads(boxes_raw or "[]")
            pallets_data = json.loads(pallets_raw or "[]")
        except json.JSONDecodeError:
            return ReceivingActionResult(status="invalid_json")
        if not isinstance(boxes_data, list):
            boxes_data = []
        if not isinstance(pallets_data, list):
            pallets_data = []
        receiving_configuration_lock = cls.receiving_configuration_lock(entries)
        default_goods_type = str(
            receiving_configuration_lock.get("goods_type")
            or status_payload.get("goods_type")
            or ""
        ).strip()
        flow_state = cls.normalize_receiving_flow_state(
            boxes_data=boxes_data,
            pallets_data=pallets_data,
            active_box=active_box,
            active_pallet=active_pallet,
            default_goods_type=default_goods_type,
        )
        receiving_date_value = cls._date_input_value(str(received_at or ""))
        if receiving_date_value:
            flow_state["received_at"] = receiving_date_value
        draft_entry = None
        latest_flow_boundary = -1
        for index, entry in enumerate(entry_list):
            entry_payload = entry.payload or {}
            if entry_payload.get("flow_closed") or entry_payload.get("flow_reopened"):
                latest_flow_boundary = index
        for entry in reversed(entry_list[latest_flow_boundary + 1 :]):
            if entry.action == "update" and (entry.payload or {}).get("flow_state"):
                draft_entry = entry
                break
        previous_flow_state = (
            dict((draft_entry.payload or {}).get("flow_state") or {})
            if draft_entry
            else {}
        )
        mixed_pallets = cls.receiving_flow_mixed_pallet_goods_type_conflicts(
            flow_state=flow_state,
            default_goods_type=default_goods_type,
        )
        if mixed_pallets:
            return ReceivingActionResult(
                status="mixed_pallet_goods_types",
                reason="receiving_mixed_pallet_goods_types",
                meta={"pallet_codes": mixed_pallets},
            )
        if receiving_configuration_lock:
            normalized_previous_flow_state = cls.normalize_receiving_flow_state(
                boxes_data=previous_flow_state.get("boxes") or [],
                pallets_data=previous_flow_state.get("pallets") or [],
                active_box=previous_flow_state.get("activeBox") or "",
                active_pallet=previous_flow_state.get("activePallet") or "",
                default_goods_type=default_goods_type,
            )
            goods_type_conflicts = cls.receiving_flow_goods_type_conflicts(
                previous_flow_state=normalized_previous_flow_state,
                next_flow_state=flow_state,
                locked_goods_type=default_goods_type,
            )
            if goods_type_conflicts:
                return ReceivingActionResult(
                    status="goods_type_locked",
                    reason="receiving_goods_type_locked",
                    meta={"container_codes": goods_type_conflicts},
                )
        locked_pallet_changes = cls.receiving_flow_locked_pallet_changes(
            order_id=order_key,
            previous_flow_state=previous_flow_state,
            next_flow_state=flow_state,
        )
        if locked_pallet_changes:
            return ReceivingActionResult(
                status="pallet_composition_locked",
                meta={"pallet_codes": locked_pallet_changes},
            )
        incoming_draft_version = cls._parse_flow_client_version(draft_version)
        previous_draft_version = cls._parse_flow_client_version(
            (draft_entry.payload or {}).get("flow_client_version") if draft_entry else 0
        )
        raw_base_version = str(draft_base_version or "").strip()
        has_explicit_base_version = bool(raw_base_version)
        incoming_base_version = cls._parse_flow_client_version(raw_base_version)
        if has_explicit_base_version:
            stale_draft = bool(
                draft_entry
                and flow_state != previous_flow_state
                and incoming_base_version != previous_draft_version
            )
        else:
            # Compatibility for pages opened before the server-side revision
            # protocol was deployed. New pages always send draft_base_version.
            stale_draft = previous_draft_version and (
                incoming_draft_version < previous_draft_version
                or (
                    incoming_draft_version == previous_draft_version
                    and flow_state != previous_flow_state
                )
            )
        if stale_draft:
            return ReceivingActionResult(
                status="saved",
                payload=draft_entry.payload or {},
                meta={
                    "updated_existing": True,
                    "stale_ignored": True,
                    "flow_client_version": previous_draft_version,
                },
            )
        latest = entries[-1] if entries else None
        payload = {"flow_state": flow_state}
        if has_explicit_base_version:
            payload_draft_version = (
                previous_draft_version
                if draft_entry and flow_state == previous_flow_state
                else previous_draft_version + 1
            )
        else:
            payload_draft_version = incoming_draft_version or previous_draft_version
        if payload_draft_version:
            payload["flow_client_version"] = payload_draft_version
        if receiving_date_value:
            payload["received_at"] = receiving_date_value
        if draft_entry:
            draft_entry.payload = payload
            draft_entry.description = "Черновик приемки потоком"
            draft_entry.save(update_fields=["payload", "description"])
        else:
            log_order_action(
                "update",
                order_id=order_id,
                order_type="receiving",
                user=cls._authenticated_user(user),
                agency=latest.agency if latest else None,
                description="Черновик приемки потоком",
                payload=payload,
            )
        return ReceivingActionResult(
            status="saved",
            payload=payload,
            meta={"updated_existing": bool(draft_entry)},
        )

    @classmethod
    def prepare_receiving_placement_close(
        cls,
        *,
        order_id: str,
        entries,
        boxes_raw: str,
        pallets_raw: str,
        order_type: str = "receiving",
    ) -> ReceivingPlacementPreparationResult:
        if not order_id or not entries:
            return ReceivingPlacementPreparationResult(status="missing", reason="missing")
        receiving_entry = cls._find_act_entry(entries, "receiving", "акт приемки")
        act_items = (receiving_entry.payload or {}).get("act_items") if receiving_entry else []
        if not act_items:
            return ReceivingPlacementPreparationResult(status="invalid", reason="missing_act_items")
        try:
            boxes_data = json.loads(boxes_raw or "[]")
            pallets_data = json.loads(pallets_raw or "[]")
        except json.JSONDecodeError:
            return ReceivingPlacementPreparationResult(status="invalid", reason="invalid_json")
        if not isinstance(boxes_data, list):
            boxes_data = []
        if isinstance(pallets_data, list):
            pallets_data = [
                pallet
                for pallet in pallets_data
                if isinstance(pallet, dict)
                and (((pallet.get("items") or []) or (pallet.get("boxes") or [])))
            ]
        else:
            pallets_data = []
        boxes = [box for box in boxes_data if isinstance(box, dict)]
        pallets = [pallet for pallet in pallets_data if isinstance(pallet, dict)]

        totals: dict[str, dict[str, int]] = {}

        def add_total(item, qty, field):
            key = cls._item_key(
                item.get("sku"),
                item.get("name"),
                item.get("size"),
                item.get("brand"),
                item.get("color"),
            )
            entry = totals.setdefault(key, {"box": 0, "pallet": 0, "total": 0})
            entry[field] += qty
            entry["total"] += qty

        for box in boxes:
            for item in (box.get("items") or []):
                qty = cls._parse_qty_value((item or {}).get("qty")) or 0
                add_total(item or {}, qty, "box")
        for pallet in pallets:
            for item in (pallet.get("items") or []):
                qty = cls._parse_qty_value((item or {}).get("qty")) or 0
                add_total(item or {}, qty, "pallet")

        placement_items = []
        for item in act_items:
            key = cls._item_key(
                item.get("sku_code"),
                item.get("name"),
                item.get("size"),
                item.get("brand"),
                item.get("color"),
            )
            entry = totals.get(key, {"box": 0, "pallet": 0, "total": 0})
            actual_qty = cls._parse_qty_value(item.get("actual_qty")) or 0
            if entry["total"] != actual_qty:
                return ReceivingPlacementPreparationResult(status="invalid", reason="qty_mismatch")
            placement_items.append(
                {
                    "sku_code": item.get("sku_code"),
                    "name": item.get("name"),
                    "brand": item.get("brand"),
                    "color": item.get("color"),
                    "size": item.get("size"),
                    "actual_qty": actual_qty,
                    "box_qty": entry["box"],
                    "pallet_qty": entry["pallet"],
                    "comment": item.get("comment"),
                }
            )

        for box in boxes:
            if not box.get("sealed"):
                return ReceivingPlacementPreparationResult(status="invalid", reason="unsealed_box")

        pallet_box_codes = set()
        for pallet in pallets:
            for box_code in (pallet.get("boxes") or []):
                text = str(box_code or "").strip()
                if text:
                    pallet_box_codes.add(text)
        unassigned_boxes = [
            box
            for box in boxes
            if str(box.get("code") or "").strip() and str(box.get("code") or "").strip() not in pallet_box_codes
        ]
        if unassigned_boxes:
            return ReceivingPlacementPreparationResult(status="invalid", reason="unassigned_boxes")

        occupied_cells = StockAvailabilityService.occupied_os_cell_keys(
            exclude_order_type=order_type,
            exclude_order_id=str(order_id or ""),
        )
        used_cells = set()
        normalized_pallets = []
        allowed_zones = {"PR", "OTG", "MR", "OS"}
        for pallet in pallets:
            if not pallet.get("sealed"):
                return ReceivingPlacementPreparationResult(status="invalid", reason="unsealed_pallet")
            location = cls._normalize_receiving_move_location(pallet.get("location"), pallet)
            zone = cls._normalize_zone_code(location.get("zone") or "PR")
            if zone not in allowed_zones:
                zone = "PR"
            row = cls._parse_int_value(location.get("row"))
            section = cls._parse_int_value(location.get("section"))
            tier = cls._parse_int_value(location.get("tier"))
            cell = cls._parse_int_value(location.get("cell"))
            if zone == "MR":
                if not row:
                    return ReceivingPlacementPreparationResult(status="invalid", reason="invalid_mr_location")
            elif zone == "OS":
                if not (row and section and tier and cell):
                    return ReceivingPlacementPreparationResult(status="invalid", reason="invalid_os_location")
                key = (row, section, tier, cell)
                if key in occupied_cells or key in used_cells:
                    return ReceivingPlacementPreparationResult(status="invalid", reason="occupied_os_location")
                used_cells.add(key)
            normalized_pallets.append(
                {
                    **pallet,
                    "location": {
                        "zone": zone,
                        "row": row if zone in {"MR", "OS"} else "",
                        "section": section if zone == "OS" else "",
                        "tier": tier if zone == "OS" else "",
                        "cell": cell if zone == "OS" else "",
                    },
                }
            )

        has_closed_act = any(
            (entry.payload or {}).get("act") == "placement"
            and ((entry.payload or {}).get("act_state") or "closed") == "closed"
            for entry in entries
        )
        return ReceivingPlacementPreparationResult(
            status="ok",
            placement_items=placement_items,
            boxes=boxes,
            pallets=normalized_pallets,
            has_closed_act=has_closed_act,
        )

    @classmethod
    def build_receiving_flow_page_context(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
        session_key: str = "",
        query_params: dict | None = None,
        row_sections: dict | None = None,
        tiers: list | tuple | None = None,
        cells_per_tier: int = 0,
    ) -> dict:
        latest = entries[-1] if entries else None
        status_entry = cls._current_status_entry(entries)
        payload = cls._latest_payload(entries)
        items = payload.get("items") or []
        sku_map = cls._receiving_sku_map(latest.agency_id if latest else None, items)
        display_items = []
        for item in items:
            sku_code = str(item.get("sku_code") or "").strip()
            sku = sku_map.get(sku_code.lower()) if sku_code else None
            weight_value = item.get("weight_kg")
            if weight_value in (None, "") and sku and sku.weight_kg is not None:
                weight_value = sku.weight_kg
            display_items.append(
                {
                    "sku_code": sku_code,
                    "barcode": item.get("barcode") or cls._barcode_value_for_sku(sku, item.get("size")),
                    "name": item.get("name") or "",
                    "brand": item.get("brand") or (sku.brand if sku else ""),
                    "color": item.get("color") or (sku.color if sku else ""),
                    "size": item.get("size") or "",
                    "qty": item.get("qty") or 0,
                    "comment": item.get("comment") or "",
                    "weight_kg": str(weight_value).strip() if weight_value not in (None, "") else "",
                }
            )
        client_label, client_prefix = cls._client_display(latest.agency if latest else None)
        status_payload = status_entry.payload or {} if status_entry else {}
        receiving_configuration_lock = cls.receiving_configuration_lock(entries)
        goods_type = str(
            receiving_configuration_lock.get("goods_type")
            or status_payload.get("goods_type")
            or ""
        ).strip().lower()
        goods_type_labels = {
            "op": "Оптовый",
            "gv": "Готовый",
            "br": "Брак",
            "vz": "Возврат",
            "rh": "Расходный",
            "no": "Не обработанный",
            SHIPPING_RETURN_GOODS_TYPE: SHIPPING_RETURN_GOODS_TYPE_LABEL,
        }
        receiving_mode = str(
            receiving_configuration_lock.get("receiving_mode")
            or cls.effective_receiving_mode(status_payload)
        ).strip().lower()
        receiving_chz_service = cls.normalize_receiving_chz_service(
            status_payload.get("receiving_chz_service")
            or payload.get("receiving_chz_service")
        )
        receiving_route = cls.receiving_route_from_entries(entries)
        receiving_mode_locked_by_client = receiving_chz_service == "required"
        barcode_map = {}
        catalog_items = []
        marked_items_map = cls._receiving_marked_items_map(latest.agency_id if latest else None, items)
        if receiving_mode == "cz" and not marked_items_map and items:
            receiving_mode = "standard"
        if latest and latest.agency_id:
            catalog_cache_key = f"receiving-flow-catalog:{latest.agency_id}"
            cached_catalog = cache.get(catalog_cache_key)
            if cached_catalog is not None:
                catalog_items = [dict(item) for item in cached_catalog[0]]
                barcode_map = {key: dict(value) for key, value in cached_catalog[1].items()}
            else:
                for sku in SKU.objects.filter(agency_id=latest.agency_id, deleted=False).only(
                    "sku_code",
                    "name",
                    "brand",
                    "color",
                    "size",
                    "weight_kg",
                ):
                    catalog_items.append(
                        {
                            "sku_code": sku.sku_code,
                            "name": sku.name,
                            "brand": sku.brand or "",
                            "color": sku.color or "",
                            "size": sku.size,
                            "weight_kg": str(sku.weight_kg).strip() if sku.weight_kg is not None else "",
                        }
                    )
                for barcode in SKUBarcode.objects.select_related("sku").filter(
                    sku__agency_id=latest.agency_id,
                    sku__deleted=False,
                ):
                    value = (barcode.value or "").strip()
                    if not value:
                        continue
                    sku = barcode.sku
                    barcode_map[value] = {
                        "sku_code": sku.sku_code,
                        "name": sku.name,
                        "brand": sku.brand or "",
                        "color": sku.color or "",
                        "size": (barcode.size or sku.size or "").strip(),
                        "weight_kg": str(sku.weight_kg).strip() if sku.weight_kg is not None else "",
                    }
                cache.set(catalog_cache_key, (catalog_items, barcode_map), 15)
        if goods_type == SHIPPING_RETURN_GOODS_TYPE:
            source_keys = {
                (
                    str(item.get("sku_code") or item.get("sku") or "").strip().casefold(),
                    str(item.get("size") or "").strip().casefold(),
                )
                for item in items
                if isinstance(item, dict)
            }
            catalog_items = [
                item
                for item in catalog_items
                if (
                    str(item.get("sku_code") or "").strip().casefold(),
                    str(item.get("size") or "").strip().casefold(),
                )
                in source_keys
            ]
            barcode_map = {
                barcode: item
                for barcode, item in barcode_map.items()
                if (
                    str(item.get("sku_code") or "").strip().casefold(),
                    str(item.get("size") or "").strip().casefold(),
                )
                in source_keys
            }
        placement_entry = cls._find_act_entry(entries, "placement", "акт размещения")
        placement_payload = placement_entry.payload if placement_entry else {}
        if not isinstance(placement_payload, dict):
            placement_payload = {}
        placement_pallets = placement_payload.get("act_pallets") or []
        if not isinstance(placement_pallets, list):
            placement_pallets = []
        flow_state = cls.find_receiving_flow_state(entries)
        if not flow_state or not (flow_state.get("boxes") or flow_state.get("pallets")):
            if placement_entry:
                placement_boxes = placement_payload.get("act_boxes") or []
                if placement_boxes or placement_pallets:
                    active_box = next((box.get("code") for box in placement_boxes if not box.get("sealed")), "")
                    active_pallet = next((pallet.get("code") for pallet in placement_pallets if not pallet.get("sealed")), "")
                    flow_state = {
                        "boxes": placement_boxes,
                        "pallets": placement_pallets,
                        "activeBox": active_box or "",
                        "activePallet": active_pallet or "",
                    }
        flow_locked = cls._flow_closed(entries)
        flow_reopened = cls._flow_reopened(entries)
        receiving_result = cls._receiving_state_result(entries)
        warehouse_move_panel = ReceivingWarehouseMovePanelResult()
        warehouse_move_progress = warehouse_move_panel.progress
        warehouse_move_status = ""
        warehouse_move_created_count = 0
        warehouse_move_skipped_count = 0
        warehouse_move_missing_count = 0
        warehouse_move_error = ""
        show_warehouse_move_action = False
        occupied_cells = {}
        act_entry = cls._find_document_act_entry(entries, "receiving", "акт приемки")
        act_print_url = ""
        if act_entry and flow_locked:
            act_print_url = f"/orders/receiving/{order_id}/act/print/?return=/orders/receiving/{order_id}/flow/"
        receiving_date_value = cls._date_input_value(str(payload.get("received_at") or ""))
        if not receiving_date_value and isinstance(flow_state, dict):
            receiving_date_value = cls._date_input_value(str(flow_state.get("received_at") or ""))
        if not receiving_date_value and act_entry:
            receiving_date_value = cls._date_input_value(str((act_entry.payload or {}).get("received_at") or ""))
        if not receiving_date_value:
            receiving_date_value = timezone.localdate().isoformat()
        flow_state_nomenclature_reconciled = False
        if not flow_locked:
            flow_state, flow_state_nomenclature_reconciled = cls._reconcile_receiving_flow_nomenclature(
                agency=latest.agency if latest else None,
                flow_state=flow_state,
                goods_type=goods_type,
            )
        flow_pallets_for_status = flow_state.get("pallets") if isinstance(flow_state, dict) else placement_pallets
        flow_pallet_takeout_statuses = cls.build_receiving_flow_pallet_takeout_statuses(
            order_id,
            flow_pallets_for_status,
        )
        flow_actual_pallet_locations = cls.build_receiving_flow_actual_pallet_locations(
            order_id,
            flow_pallets_for_status,
        )
        scanner_agents = []
        preferred_agent_id = ""
        preferred_agent_locked = False
        try:
            now = timezone.now()
            online_threshold = timezone.now() - timedelta(seconds=30)
            active_context = None
            if getattr(user, "is_authenticated", False):
                context_qs = AgentContext.objects.filter(
                    user=user,
                    active=True,
                    expires_at__gt=now,
                )
                if order_id:
                    try:
                        order_value = int(str(order_id).strip())
                    except (TypeError, ValueError):
                        order_value = None
                    if order_value is not None:
                        context_qs = context_qs.filter(order_id=order_value)
                session_key_text = str(session_key or "").strip()
                if session_key_text:
                    active_context = (
                        context_qs.filter(session_key=session_key_text)
                        .order_by("-last_seen", "-updated_at")
                        .first()
                    )
                    if active_context and active_context.agent_id:
                        preferred_agent_locked = True
                if not active_context:
                    active_context_count = context_qs.count()
                    if active_context_count == 1:
                        active_context = context_qs.order_by("-last_seen", "-updated_at").first()
                        if active_context and active_context.agent_id:
                            preferred_agent_locked = True
                if active_context and active_context.agent_id:
                    preferred_agent_id = str(active_context.agent_id).strip()
            all_agents = []
            for agent in DeviceAgent.objects.all().order_by("-last_seen", "-updated_at"):
                is_online = bool(agent.last_seen and agent.last_seen >= online_threshold)
                all_agents.append(
                    {
                        "agent_id": agent.agent_id,
                        "title": agent.name or agent.host or agent.agent_id,
                        "status": "онлайн" if is_online else "нет связи",
                        "is_online": is_online,
                        "last_seen": agent.last_seen.isoformat() if agent.last_seen else "",
                    }
                )
            scanner_agents = all_agents
        except Exception:
            scanner_agents = []
            preferred_agent_id = ""
            preferred_agent_locked = False
        try:
            from labels.utils import load_label_settings

            label_settings = load_label_settings()
        except Exception:
            label_settings = {}
        status_audience = "storekeeper" if role == "storekeeper" else "default"
        if not receiving_result:
            receiving_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
                order_id=str(order_id or ""),
                agency=latest.agency if latest else None,
                payload=payload,
            )
        known_box_metrics = cls.build_known_box_metrics_defaults(
            agency=latest.agency if latest else None,
            mode="receiving",
            items=display_items,
            flow_state=flow_state,
        )
        display_status_label = receiving_result.label_for(status_audience)
        has_open_flow = (
            not flow_locked
            and isinstance(flow_state, dict)
            and bool((flow_state.get("boxes") or []) or (flow_state.get("pallets") or []))
        )
        if has_open_flow:
            display_status_label = "В процессе приемки"
        elif flow_reopened and not flow_locked:
            display_status_label = "Внесение изменений"
        concrete_receiving_location_required = cls.requires_concrete_receiving_location(entries)
        receiving_location_options = []
        receiving_location_selected_code = ""
        for entry in reversed(entries):
            entry_payload = entry.payload if isinstance(entry.payload, dict) else {}
            receiving_location_selected_code = str(
                entry_payload.get("receiving_location_code") or ""
            ).strip()
            if receiving_location_selected_code:
                break
        if concrete_receiving_location_required and not flow_locked:
            occupied_location_ids = occupied_operational_location_ids(
                warehouse_code="MSK",
                zone_code="PR",
                exclude_context_type="receiving",
                exclude_context_id=str(order_id or "").strip(),
            )
            receiving_location_options = list(
                receiving_locations()
                .exclude(id__in=occupied_location_ids)
                .order_by("location_code", "display_name")
                .values("location_code", "display_name")
            )
        return {
            "order_id": order_id,
            "client_label": client_label,
            "client_prefix": client_prefix,
            "status_label": display_status_label,
            "goods_type": goods_type,
            "goods_type_label": goods_type_labels.get(goods_type, ""),
            "goods_type_choices": [
                choice
                for choice in cls.RECEIVING_GOODS_TYPE_LABELS.items()
                if choice[0] != SHIPPING_RETURN_GOODS_TYPE or goods_type == SHIPPING_RETURN_GOODS_TYPE
            ],
            "shipping_return_order_number": str(
                status_payload.get("shipping_return_order_number") or ""
            ).strip(),
            "shipping_return_order_display": (
                format_order_number(
                    "shipping",
                    status_payload.get("shipping_return_order_number"),
                )
                if status_payload.get("shipping_return_order_number")
                else ""
            ),
            "receiving_mode": receiving_mode,
            "receiving_mode_label": "Приемка с ЧЗ" if receiving_mode == "cz" else "Обычная приемка",
            "receiving_chz_service": receiving_chz_service,
            "receiving_chz_service_label": cls.receiving_chz_service_label(
                receiving_chz_service
            ),
            "receiving_route": receiving_route,
            "receiving_route_label": cls.receiving_route_label(receiving_route),
            "receiving_mode_locked_by_client": receiving_mode_locked_by_client,
            "items": display_items,
            "barcode_map": barcode_map,
            "catalog_items": catalog_items,
            "marked_items": list(marked_items_map.values()),
            "marking_scan_url": f"/marking/receiving/{order_id}/scan/" if marked_items_map else "",
            "flow_state": flow_state,
            "flow_state_nomenclature_reconciled": flow_state_nomenclature_reconciled,
            "flow_pallet_takeout_statuses": flow_pallet_takeout_statuses,
            "flow_actual_pallet_locations": flow_actual_pallet_locations,
            "flow_locked": flow_locked,
            "show_warehouse_move_action": show_warehouse_move_action,
            "scanner_agents": scanner_agents,
            "preferred_agent_id": preferred_agent_id,
            "preferred_agent_locked": preferred_agent_locked,
            "label_settings": label_settings,
            "known_box_metrics": known_box_metrics,
            "can_send_to_warehouse_action": warehouse_move_panel.can_send,
            "warehouse_move_progress": warehouse_move_progress,
            "warehouse_total_pallets": int(warehouse_move_progress.get("total_pallets") or 0),
            "warehouse_done_pallets": int(warehouse_move_progress.get("done_count") or 0),
            "warehouse_created_pallets": int(warehouse_move_progress.get("created_count") or 0),
            "warehouse_in_progress_pallets": int(warehouse_move_progress.get("in_progress_count") or 0),
            "warehouse_not_created_pallets": int(warehouse_move_progress.get("not_created_count") or 0),
            "warehouse_move_status": warehouse_move_status,
            "warehouse_move_created_count": warehouse_move_created_count,
            "warehouse_move_skipped_count": warehouse_move_skipped_count,
            "warehouse_move_missing_count": warehouse_move_missing_count,
            "warehouse_move_rows": warehouse_move_panel.rows,
            "warehouse_move_error": warehouse_move_error,
            "occupied_cells": occupied_cells,
            "current_agency_id": latest.agency_id if latest else 0,
            "act_print_url": act_print_url,
            "receiving_date_value": receiving_date_value,
            "can_change_goods_type": (
                role == "storekeeper"
                and not flow_locked
                and not receiving_configuration_lock
            ),
            "receiving_configuration_locked": bool(receiving_configuration_lock),
            "can_allow_pallet_placement": role == "storekeeper" and not flow_locked,
            "concrete_receiving_location_required": concrete_receiving_location_required,
            "receiving_location_options": receiving_location_options,
            "receiving_location_selected_code": receiving_location_selected_code,
        }

    @classmethod
    def build_receiving_act_page_context(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        client_view: bool = False,
        client_agency=None,
        cabinet_url: str = "",
    ) -> dict:
        latest = entries[-1] if entries else None
        status_entry = cls._current_status_entry(entries)
        status_payload = status_entry.payload or {} if status_entry else {}
        payload = cls._latest_payload(entries)
        order_title = resolve_order_title(
            "receiving",
            order_id,
            payload=payload,
            entries=entries,
            created_at=entries[0].created_at if entries else None,
            variant="base",
        )
        items = payload.get("items") or []
        act_entry = cls._find_act_entry(entries, "receiving", "акт приемки")
        act_items = (act_entry.payload or {}).get("act_items") if act_entry else []
        flow_closed = cls._flow_closed(entries)
        act_label = ((act_entry.payload or {}).get("act_label") or "Акт приемки") if act_entry else "Акт приемки"
        act_documents = []
        if act_entry and flow_closed:
            act_documents = [
                {"label": "Акт приемки печатная форма", "url": f"/orders/receiving/{order_id}/act/print/"},
                {"label": "МХ-1", "url": f"/orders/receiving/{order_id}/act/mx1/print/"},
            ]
            if role == "storekeeper":
                act_documents = [act_documents[0]]
        can_submit = role == "storekeeper" and cls.can_create_receiving_act(entries, role="storekeeper")
        if client_view:
            can_submit = False
        base_items = act_items or items
        sku_ids = set()
        sku_codes = set()
        for item in base_items:
            sku_code = str(item.get("sku_code") or "").strip()
            if sku_code:
                sku_codes.add(sku_code)
            sku_id_raw = item.get("sku_id")
            if sku_id_raw:
                try:
                    sku_ids.add(int(sku_id_raw))
                except (TypeError, ValueError):
                    pass
        sku_by_id = {}
        if sku_ids:
            for sku in SKU.objects.filter(id__in=sku_ids, deleted=False).prefetch_related("barcodes"):
                sku_by_id[sku.id] = sku
        sku_by_code = {}
        if sku_codes:
            sku_qs = SKU.objects.filter(sku_code__in=sku_codes, deleted=False)
            if latest and latest.agency_id:
                sku_qs = sku_qs.filter(agency_id=latest.agency_id)
            for sku in sku_qs.prefetch_related("barcodes"):
                sku_by_code.setdefault(sku.sku_code, sku)
        base_display_items = []
        for item in base_items:
            actual_value = "" if not act_items else item.get("actual_qty")
            planned_value = item.get("planned_qty") if act_items else item.get("qty")
            name = item.get("name")
            brand = item.get("brand")
            color = item.get("color")
            size = item.get("size")
            sku_code_value = item.get("sku_code") or ""
            sku_id_value = item.get("sku_id")
            sku_id = None
            if sku_id_value not in (None, ""):
                try:
                    sku_id = int(sku_id_value)
                except (TypeError, ValueError):
                    sku_id = None
            sku = sku_by_id.get(sku_id) or sku_by_code.get(sku_code_value)
            barcode_value = item.get("barcode") or cls._barcode_value_for_sku(sku, size)
            base_display_items.append(
                {
                    "sku_code": sku_code_value or "-",
                    "barcode": barcode_value,
                    "name": name or "-",
                    "brand": brand or (sku.brand if sku else "") or "-",
                    "color": color or (sku.color if sku else "") or "-",
                    "size": size or "-",
                    "planned_qty": planned_value if planned_value not in (None, "") else 0,
                    "actual_qty": actual_value if actual_value is not None else "",
                    "comment": item.get("comment") or "",
                }
            )
        client_label, client_prefix = cls._client_display(latest.agency if latest else None)
        sku_options = []
        sku_name_options = []
        barcode_options = []
        barcode_map = {}
        if latest and latest.agency_id:
            name_seen = set()
            barcode_seen = set()
            sku_seen = set()
            for sku in SKU.objects.filter(agency_id=latest.agency_id, deleted=False).prefetch_related("barcodes").order_by("sku_code"):
                barcode_values = []
                for barcode in sku.barcodes.all():
                    value = (barcode.value or "").strip()
                    if not value:
                        continue
                    barcode_values.append(value)
                    if value not in barcode_seen:
                        barcode_seen.add(value)
                        barcode_options.append(value)
                    barcode_map.setdefault(
                        value,
                        {
                            "sku": sku.sku_code,
                            "sku_code": sku.sku_code,
                            "name": sku.name,
                            "brand": sku.brand or "",
                            "color": sku.color or "",
                            "size": (barcode.size or sku.size or "").strip(),
                        },
                    )
                sku_options.append(
                    {
                        "code": sku.sku_code,
                        "name": sku.name,
                        "brand": sku.brand or "",
                        "color": sku.color or "",
                        "barcodes_joined": "|".join(barcode_values),
                    }
                )
                sku_seen.add(sku.sku_code)
                if sku.name and sku.name not in name_seen:
                    name_seen.add(sku.name)
                    sku_name_options.append(sku.name)
        placement_entry = cls._find_document_act_entry(entries, "placement", "Р°РєС‚ СЂР°Р·РјРµС‰РµРЅРёСЏ")
        placement_payload = placement_entry.payload if placement_entry and isinstance(placement_entry.payload, dict) else {}
        default_goods_type = ""
        for payload_source in ((act_entry.payload or {}) if act_entry else {}, placement_payload, status_payload, payload):
            if not isinstance(payload_source, dict):
                continue
            default_goods_type = cls._normalize_flow_goods_type(payload_source.get("goods_type"), default_goods_type)
            if default_goods_type:
                break
        special_item_rows = cls.build_special_item_rows(
            boxes=placement_payload.get("act_boxes") or [],
            default_goods_type=default_goods_type,
        )
        display_items = cls.split_receiving_act_rows_by_goods_type(
            base_display_items,
            special_rows=special_item_rows,
            planned_key="planned_qty",
            actual_key="actual_qty",
        )
        for item in display_items:
            planned_value = item.get("planned_qty")
            actual_value = item.get("actual_qty")
            item["planned_qty"] = planned_value if planned_value not in (None, "") else "-"
            item["actual_qty"] = actual_value if actual_value not in (None, "") else ""
        special_boxes = cls.build_special_box_rows(
            boxes=placement_payload.get("act_boxes") or [],
            pallets=placement_payload.get("act_pallets") or [],
            default_goods_type=default_goods_type,
        )
        arrival_value = payload.get("eta_at")
        vehicle_value = payload.get("vehicle_number")
        driver_phone_value = payload.get("driver_phone")
        if act_entry:
            act_payload = act_entry.payload or {}
            arrival_value = act_payload.get("eta_at") or arrival_value
            vehicle_value = act_payload.get("vehicle_number") or vehicle_value
            driver_phone_value = act_payload.get("driver_phone") or driver_phone_value
        arrival_input = ""
        if arrival_value:
            try:
                arrival_input = parse_business_datetime(str(arrival_value)).strftime("%Y-%m-%dT%H:%M")
            except (TypeError, ValueError):
                arrival_input = ""
        status_audience = "client" if client_view else ("storekeeper" if role == "storekeeper" else "default")
        receiving_status_label = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(order_id or ""),
            agency=latest.agency if latest else client_agency,
            payload=payload,
        ).label_for(status_audience)
        resolved_cabinet_url = cabinet_url
        if client_view and client_agency:
            resolved_cabinet_url = f"/client/dashboard/?client={client_agency.id}"
        return {
            "order_id": order_id,
            "order_title": order_title,
            "client_label": client_label,
            "client_prefix": client_prefix,
            "status_label": receiving_status_label,
            "cabinet_url": resolved_cabinet_url,
            "client_view": client_view,
            "client_param": client_agency.id if client_agency else "",
            "arrival_at": _format_datetime_value(arrival_value),
            "arrival_value": _format_datetime_value(arrival_value),
            "arrival_input": arrival_input,
            "vehicle_number": vehicle_value or "-",
            "vehicle_value": vehicle_value or "",
            "driver_phone": _format_payload_value(driver_phone_value),
            "can_submit": can_submit,
            "can_add_items": can_submit,
            "act_exists": bool(act_entry),
            "act_label": act_label,
            "act_documents": act_documents,
            "items": display_items,
            "sku_options": sku_options,
            "sku_name_options": sku_name_options,
            "barcode_options": barcode_options,
            "barcode_map": barcode_map,
            "agency_id": latest.agency_id if latest else "",
            "special_boxes": special_boxes,
        }

    @classmethod
    def build_receiving_placement_page_context(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        order_type: str = "receiving",
    ) -> dict:
        latest = entries[-1] if entries else None
        status_entry = cls._current_status_entry(entries)
        status_payload = status_entry.payload or {} if status_entry else {}
        receiving_act = cls._find_act_entry(entries, "receiving", "акт приемки")
        placement_act = cls._find_act_entry(entries, "placement", "акт размещения")
        latest_payload = latest.payload or {} if latest else {}
        placement_payload = placement_act.payload or {} if placement_act else {}
        receiving_items = (receiving_act.payload or {}).get("act_items") if receiving_act else []
        placement_items = (placement_act.payload or {}).get("act_items") if placement_act else []
        display_items = []
        for item in placement_items or receiving_items:
            display_items.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "-",
                    "size": item.get("size") or "-",
                    "actual_qty": item.get("actual_qty") or 0,
                    "box_qty": item.get("box_qty") or 0,
                    "pallet_qty": item.get("pallet_qty") or 0,
                }
            )
        catalog_items = []
        remaining_items = []
        for item in receiving_items:
            catalog_items.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "",
                    "size": item.get("size") or "",
                }
            )
            remaining_items.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "",
                    "size": item.get("size") or "",
                    "actual_qty": item.get("actual_qty") or 0,
                }
            )
        barcode_map = {}
        if latest and latest.agency_id and receiving_items:
            sku_codes = {
                (item.get("sku_code") or "").strip()
                for item in receiving_items
                if (item.get("sku_code") or "").strip()
            }
            if sku_codes:
                for sku in SKU.objects.filter(
                    agency_id=latest.agency_id,
                    sku_code__in=sku_codes,
                    deleted=False,
                ).prefetch_related("barcodes"):
                    for barcode in sku.barcodes.all():
                        value = (barcode.value or "").strip()
                        if not value:
                            continue
                        barcode_map.setdefault(
                            value,
                            {
                                "sku": sku.sku_code,
                                "name": sku.name,
                                "size": (barcode.size or sku.size or "").strip(),
                            },
                        )
                    sku_code_barcode = (sku.code or "").strip()
                    if sku_code_barcode:
                        barcode_map.setdefault(
                            sku_code_barcode,
                            {
                                "sku": sku.sku_code,
                                "name": sku.name,
                                "size": (sku.size or "").strip(),
                            },
                        )
        client_label, _client_prefix = cls._client_display(latest.agency if latest else None)
        can_submit = role == "storekeeper"
        act_state = (placement_act.payload or {}).get("act_state") or "closed" if placement_act else "open"
        signed_by_storekeeper = cls._act_storekeeper_signed(entries)
        receiving_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(order_id or ""),
            agency=latest.agency if latest else None,
            payload=status_payload,
        ) if latest else None
        can_open_act = WarehouseActionPolicy.can_open_receiving_placement(
            receiving_result,
            role=role,
            act_state=act_state,
            signed_by_storekeeper=signed_by_storekeeper,
        ).allowed
        boxes_data = (placement_act.payload or {}).get("act_boxes") if placement_act else []
        pallets_data = (placement_act.payload or {}).get("act_pallets") if placement_act else []
        occupied_cells = StockAvailabilityService.occupied_os_cells(
            exclude_order_type=order_type,
            exclude_order_id=order_id,
            include_agency=True,
        )
        audience = "storekeeper" if role == "storekeeper" else "default"
        status_label = receiving_result.label_for(audience) if receiving_result else "-"
        goods_type = ""
        goods_type_label = ""
        for payload_source in (placement_payload, latest_payload, status_payload):
            if not isinstance(payload_source, dict):
                continue
            goods_type = str(payload_source.get("goods_type") or "").strip().lower() or goods_type
            goods_type_label = str(payload_source.get("goods_type_label") or "").strip() or goods_type_label
            if goods_type and goods_type_label:
                break
        if order_type == "processing" and not goods_type and not goods_type_label:
            goods_type = "gv"
        if not goods_type_label and goods_type:
            goods_type_label = cls.RECEIVING_GOODS_TYPE_LABELS.get(goods_type, goods_type)
        return {
            "order_id": order_id,
            "client_label": client_label,
            "status_label": status_label,
            "goods_type": goods_type,
            "goods_type_label": goods_type_label,
            "order_detail_url": f"/orders/{order_type}/{order_id}/",
            "can_submit": can_submit,
            "can_open_act": can_open_act,
            "signed_by_storekeeper": signed_by_storekeeper,
            "act_exists": bool(placement_act),
            "act_state": act_state,
            "items": display_items,
            "catalog_items": catalog_items,
            "remaining_items": remaining_items,
            "barcode_map": barcode_map,
            "boxes_data": boxes_data,
            "pallets_data": pallets_data,
            "occupied_cells": occupied_cells,
            "current_agency_id": latest.agency_id if latest else 0,
        }

    @classmethod
    def log_receiving_flow_box_action(
        cls,
        *,
        order_id: str,
        action: str,
        payload: dict | None,
        user=None,
    ) -> ReceivingFlowBoxActionResult:
        action_key = str(action or "").strip().lower()
        action_kind, action_label = cls.FLOW_BOX_ACTIONS.get(action_key, ("", ""))
        if not action_label:
            return ReceivingFlowBoxActionResult(status="invalid_action", reason="invalid_action")
        source = payload if isinstance(payload, dict) else {}
        box_code = (source.get("box_code") or "").strip()
        box_codes = [
            str(value).strip()
            for value in (source.get("box_codes") or [])
            if str(value or "").strip()
        ]
        if box_code and box_code not in box_codes:
            box_codes.insert(0, box_code)
        if action_key in {"edit", "delete"} and not box_code:
            return ReceivingFlowBoxActionResult(status="missing_box", reason="missing_box")
        if action_key in {"delete_batch", "move_batch", "print_batch"} and not box_codes:
            return ReceivingFlowBoxActionResult(status="missing_boxes", reason="missing_boxes")
        snapshot = {
            "order_id": str(order_id or ""),
            "box_code": box_code,
            "previous_box_code": (source.get("previous_box_code") or "").strip(),
            "new_box_code": (source.get("new_box_code") or "").strip(),
            "box_codes": box_codes,
            "box_count": len(box_codes) if box_codes else (1 if box_code else 0),
            "pallet_code": (source.get("pallet_code") or "").strip(),
            "pallet_index": cls._parse_int_value(source.get("pallet_index")),
            "box_index": cls._parse_int_value(source.get("box_index")),
            "total_qty": cls._parse_int_value(source.get("total_qty")),
            "items": source.get("items") if isinstance(source.get("items"), list) else [],
            "previous_active_box": (source.get("previous_active_box") or "").strip(),
            "new_active_box": (source.get("new_active_box") or "").strip(),
            "source_pallet_code": (source.get("source_pallet_code") or "").strip(),
            "source_pallet_index": cls._parse_int_value(source.get("source_pallet_index")),
            "target_pallet_code": (source.get("target_pallet_code") or "").strip(),
            "target_pallet_index": cls._parse_int_value(source.get("target_pallet_index")),
            "create_new_pallet": bool(source.get("create_new_pallet")),
            "boxes": source.get("boxes") if isinstance(source.get("boxes"), list) else [],
            "previous_goods_type": cls._normalize_flow_goods_type(source.get("previous_goods_type")),
            "new_goods_type": cls._normalize_flow_goods_type(source.get("new_goods_type")),
        }
        from audit.models import OrderAuditEntry

        entry = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .select_related("agency")
            .order_by("-created_at")
            .first()
        )
        if action_key in {"edit", "delete"}:
            description = f"{action_label} {box_code} (заявка {order_id})"
            if snapshot["pallet_index"] and snapshot["box_index"]:
                description += f", палета {snapshot['pallet_index']}, короб {snapshot['box_index']}"
        else:
            description = f"{action_label} ({snapshot['box_count']} шт., заявка {order_id})"
            if snapshot["source_pallet_index"]:
                description += f", палета {snapshot['source_pallet_index']}"
            if snapshot["target_pallet_index"]:
                description += f" -> палета {snapshot['target_pallet_index']}"
        log_staff_overaction(
            action_kind,
            user=cls._authenticated_user(user),
            agency=entry.agency if entry else None,
            description=description,
            snapshot=snapshot,
        )
        return ReceivingFlowBoxActionResult(
            status="logged",
            action_kind=action_kind,
            action_label=action_label,
            snapshot=snapshot,
        )

    @classmethod
    def parse_receiving_warehouse_destinations(
        cls,
        raw,
    ) -> tuple[dict[str, dict] | None, str]:
        return parse_putaway_destinations(
            raw,
            allowed_zones={"PR", "MR", "OS"},
            allowed_zones_label="PR, MR или OS",
        )

    @staticmethod
    def _placement_payload_has_pallets(payload: dict) -> bool:
        if not isinstance(payload, dict):
            return False
        pallets = payload.get("act_pallets")
        return isinstance(pallets, list) and bool(pallets)

    @classmethod
    def _receiving_flow_placement_payload(
        cls,
        entries,
        *,
        pallet_codes: set[str] | None = None,
    ) -> dict:
        flow_state = cls.find_receiving_flow_state(entries)
        if not isinstance(flow_state, dict):
            return {}

        boxes = flow_state.get("boxes") if isinstance(flow_state.get("boxes"), list) else []
        pallets = flow_state.get("pallets") if isinstance(flow_state.get("pallets"), list) else []
        allowed_codes = {
            str(code or "").strip()
            for code in (pallet_codes or set())
            if str(code or "").strip()
        }
        restrict_to_codes = pallet_codes is not None

        boxes_by_code: dict[str, dict] = {}
        for box in boxes:
            if not isinstance(box, dict):
                continue
            box_code = str(box.get("code") or "").strip()
            if not box_code:
                continue
            box_copy = dict(box)
            box_copy["items"] = [dict(item) for item in (box.get("items") or []) if isinstance(item, dict)]
            boxes_by_code[box_code] = box_copy

        selected_pallets: list[dict] = []
        selected_box_codes: set[str] = set()
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            pallet_code = str(pallet.get("code") or "").strip()
            if not pallet_code:
                continue
            if restrict_to_codes and pallet_code not in allowed_codes:
                continue
            if not bool(pallet.get("sealed")):
                continue
            pallet_boxes = [
                str(box_code or "").strip()
                for box_code in (pallet.get("boxes") or [])
                if str(box_code or "").strip()
            ]
            pallet_copy = dict(pallet)
            pallet_copy["code"] = pallet_code
            pallet_copy["boxes"] = pallet_boxes
            pallet_copy["items"] = [dict(item) for item in (pallet.get("items") or []) if isinstance(item, dict)]
            location = pallet_copy.get("location")
            if isinstance(location, dict):
                pallet_copy["location"] = dict(location)
            else:
                pallet_copy["location"] = {"zone": "PR"}
            pallet_copy["location"].setdefault("zone", "PR")
            selected_pallets.append(pallet_copy)
            selected_box_codes.update(pallet_boxes)

        if not selected_pallets:
            return {}

        selected_boxes = [
            boxes_by_code[box_code]
            for box_code in selected_box_codes
            if box_code in boxes_by_code
        ]
        payload = {
            "act": "placement",
            "act_label": "Placement act",
            "act_state": "closed",
            "act_items": [],
            "act_boxes": selected_boxes,
            "act_pallets": selected_pallets,
        }
        if any((entry.payload or {}).get("flow_reopened") for entry in entries or []):
            payload["flow_reopened"] = True
        return payload

    @classmethod
    def create_receiving_warehouse_moves(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
        destinations_by_pallet: dict[str, dict] | None = None,
        draft_token: str = "",
    ) -> ReceivingWarehouseMoveResult:
        order_key = str(order_id or "").strip()
        if not order_key or not entries:
            return ReceivingWarehouseMoveResult(status="missing")
        if cls.receiving_route_from_entries(entries) == "fbs":
            return ReceivingWarehouseMoveResult(
                status="denied",
                reason="fbs_route_uses_exact_fbs_destination",
            )
        normalized_destinations: dict[str, dict] = {}
        if isinstance(destinations_by_pallet, dict):
            for raw_pallet_code, raw_destination in destinations_by_pallet.items():
                pallet_code = str(raw_pallet_code or "").strip()
                if not pallet_code:
                    continue
                normalized_destinations[pallet_code] = normalize_putaway_location(raw_destination)
        requested_pallet_codes = set(normalized_destinations.keys()) if normalized_destinations else None
        placement_entry = cls._find_act_entry(entries, "placement", "??? ??????????")
        placement_payload = placement_entry.payload if placement_entry else {}
        if not isinstance(placement_payload, dict):
            placement_payload = {}
        placement_is_closed = (placement_payload.get("act_state") or "closed").lower() == "closed"
        if not placement_is_closed or not cls._placement_payload_has_pallets(placement_payload):
            flow_placement_payload = cls._receiving_flow_placement_payload(
                entries,
                pallet_codes=requested_pallet_codes,
            )
            if flow_placement_payload:
                placement_payload = flow_placement_payload
            elif not placement_is_closed:
                return ReceivingWarehouseMoveResult(status="missing")
        if not cls._placement_payload_has_pallets(placement_payload):
            return ReceivingWarehouseMoveResult(status="missing")
        latest = entries[-1]
        actor_name = cls._resolve_actor_name(user)
        command_role = role if role in {"storekeeper", "manager", "head_manager", "director", "admin"} else "storekeeper"
        command_result = WarehouseCommandService.create_receiving_putaway_tasks(
            order_id=order_key,
            agency=latest.agency if latest else None,
            role=command_role,
            placement_payload=placement_payload,
            requested_by=cls._authenticated_user(user),
            requested_by_name=actor_name,
            requested_by_role=role or command_role,
            destinations_by_pallet=normalized_destinations,
            latest_moves_by_pallet=cls._latest_receiving_moves_by_pallet(order_key),
            flow_closed=True,
            not_created_count=1,
            exclude_draft_token=str(draft_token or "").strip(),
        )
        return ReceivingWarehouseMoveResult(
            status=command_result.status,
            reason=command_result.reason,
            created_count=int(command_result.created_count or 0),
            skipped_existing_count=int(command_result.skipped_existing_count or 0),
            skipped_missing_destination_count=int(command_result.skipped_missing_destination_count or 0),
            total_count=int(command_result.total_count or 0),
            destinations_by_pallet=normalized_destinations,
            source_facts=list(command_result.source_facts or []),
        )

    @classmethod
    def _latest_receiving_moves_by_pallet(cls, order_id: str) -> dict[str, dict]:
        target_id = str(order_id or "").strip()
        latest: dict[str, dict] = {}
        if not target_id:
            return latest
        from audit.models import OrderAuditEntry

        entries = OrderAuditEntry.objects.filter(order_type="stock_move").order_by("-created_at")
        for entry in entries:
            payload = entry.payload or {}
            if str(payload.get("receiving_order_id") or "").strip() != target_id:
                continue
            if str(payload.get("processing_order_id") or "").strip():
                continue
            pallet_code = str(payload.get("pallet_code") or "").strip()
            if not pallet_code or pallet_code in latest:
                continue
            latest[pallet_code] = {
                "status": str(payload.get("status") or payload.get("submit_action") or "").strip().lower(),
                "status_label": str(payload.get("status_label") or "").strip(),
                "order_id": str(entry.order_id or "").strip(),
                "to_zone": payload.get("to_zone"),
                "to_row": payload.get("to_row"),
                "to_section": payload.get("to_section"),
                "to_tier": payload.get("to_tier"),
                "to_cell": payload.get("to_cell"),
            }
        return latest

    @classmethod
    def suggest_receiving_destinations(
        cls,
        pallets,
        *,
        exclude_order_type: str,
        exclude_order_id: str,
        agency_id: int | None = None,
        row_sections: dict | None = None,
        tiers: list | tuple | None = None,
        cells_per_tier: int = 0,
    ) -> dict[str, dict]:
        return suggest_putaway_destinations(
            pallets,
            exclude_order_type=exclude_order_type,
            exclude_order_id=exclude_order_id,
            agency_id=agency_id,
            row_sections=row_sections,
            tiers=tiers,
            cells_per_tier=cells_per_tier,
        )

    @classmethod
    def build_receiving_warehouse_move_progress(cls, order_id: str, placement_pallets) -> dict:
        order_key = str(order_id or "").strip()
        pallets = placement_pallets if isinstance(placement_pallets, list) else []
        pallet_codes = []
        seen = set()
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            code = str(pallet.get("code") or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            pallet_codes.append(code)
        latest_moves = cls._latest_receiving_moves_by_pallet(order_key)
        created_count = 0
        in_progress_count = 0
        done_count = 0
        canceled_count = 0
        other_count = 0
        not_created_count = 0
        for code in pallet_codes:
            status = str((latest_moves.get(code) or {}).get("status") or "").strip().lower()
            if status == "done":
                done_count += 1
            elif status == "created":
                created_count += 1
            elif status == "in_progress":
                in_progress_count += 1
            elif status in {"canceled", "cancelled"}:
                canceled_count += 1
                not_created_count += 1
            elif status:
                other_count += 1
            else:
                not_created_count += 1
        total_pallets = len(pallet_codes)
        active_count = created_count + in_progress_count
        return {
            "total_pallets": total_pallets,
            "created_count": created_count,
            "in_progress_count": in_progress_count,
            "active_count": active_count,
            "done_count": done_count,
            "canceled_count": canceled_count,
            "other_count": other_count,
            "not_created_count": not_created_count,
            "has_any_task": bool(active_count or done_count or canceled_count or other_count),
            "all_done": bool(total_pallets) and done_count >= total_pallets,
        }

    @classmethod
    def build_receiving_warehouse_move_rows(
        cls,
        order_id: str,
        placement_pallets,
        *,
        agency_id: int | None = None,
        row_sections: dict | None = None,
        tiers: list | tuple | None = None,
        cells_per_tier: int = 0,
    ) -> list[dict]:
        order_key = str(order_id or "").strip()
        pallets = placement_pallets if isinstance(placement_pallets, list) else []
        latest_moves = cls._latest_receiving_moves_by_pallet(order_key)
        suggested = cls.suggest_receiving_destinations(
            pallets,
            exclude_order_type="receiving",
            exclude_order_id=order_key,
            agency_id=agency_id,
            row_sections=row_sections,
            tiers=tiers,
            cells_per_tier=cells_per_tier,
        )
        return build_putaway_rows(
            pallets,
            latest_moves_by_pallet=latest_moves,
            suggested_destinations=suggested,
            source_label="PR · Зона приемки",
        )

    @classmethod
    def collect_receiving_warehouse_move_destinations(
        cls,
        *,
        order_id: str,
        entries,
        pallet_codes=None,
        agency_id: int | None = None,
        row_sections: dict | None = None,
        tiers: list | tuple | None = None,
        cells_per_tier: int = 0,
    ) -> dict[str, dict]:
        order_key = str(order_id or "").strip()
        if not order_key or not entries:
            return {}
        if cls.receiving_route_from_entries(entries) == "fbs":
            return {}
        source_pallets = cls._receiving_warehouse_move_pallets(entries)
        if not source_pallets:
            return {}
        resolved_agency_id = agency_id
        if resolved_agency_id is None and entries:
            latest = entries[-1]
            resolved_agency_id = getattr(latest, "agency_id", None)
        allowed_codes = None
        if pallet_codes is not None:
            allowed_codes = {
                str(raw_code or "").strip()
                for raw_code in pallet_codes
                if str(raw_code or "").strip()
            }
        destinations: dict[str, dict] = {}
        rows = cls.build_receiving_warehouse_move_rows(
            order_key,
            source_pallets,
            agency_id=resolved_agency_id,
            row_sections=row_sections,
            tiers=tiers,
            cells_per_tier=cells_per_tier,
        )
        for row in rows:
            pallet_code = str(row.get("pallet_code") or "").strip()
            if not pallet_code:
                continue
            if allowed_codes is not None and pallet_code not in allowed_codes:
                continue
            if not row.get("selectable"):
                continue
            destination = normalize_putaway_location(row.get("destination"))
            if str(destination.get("zone") or "PR").strip().upper() == "PR":
                continue
            destinations[pallet_code] = destination
        return destinations

    @classmethod
    def _receiving_warehouse_move_pallets(cls, entries):
        flow_state = cls.find_receiving_flow_state(entries)
        flow_pallets = flow_state.get("pallets") if isinstance(flow_state, dict) else None
        if isinstance(flow_pallets, list) and flow_pallets:
            return flow_pallets
        placement_entry = cls._find_act_entry(entries, "placement", "акт размещения")
        placement_payload = placement_entry.payload if placement_entry else {}
        if not isinstance(placement_payload, dict):
            placement_payload = {}
        placement_pallets = placement_payload.get("act_pallets") or []
        if isinstance(placement_pallets, list):
            return placement_pallets
        return []

    @classmethod
    def build_receiving_flow_pallet_takeout_statuses(cls, order_id: str, pallets) -> dict[str, dict]:
        order_key = str(order_id or "").strip()
        source_pallets = pallets if isinstance(pallets, list) else []
        pallet_codes = []
        sealed_by_code = {}
        for pallet in source_pallets:
            if not isinstance(pallet, dict):
                continue
            code = str(pallet.get("code") or "").strip()
            if not code:
                continue
            pallet_codes.append(code)
            sealed_by_code[code.casefold()] = bool(pallet.get("sealed"))
        if not order_key or not pallet_codes:
            return {}

        permission_entries = list(
            OrderAuditEntry.objects.filter(order_type="receiving", order_id=order_key)
            .only("payload")
            .order_by("created_at", "id")
        )
        placement_permissions = cls._pallet_placement_permissions_from_entries(permission_entries)
        flow_closed = cls._flow_closed(permission_entries)
        materialized_codes = cls.materialized_pallet_codes(
            order_id=order_key,
            entries=permission_entries,
        )

        snapshots = (
            WarehouseStockSnapshot.objects.filter(
                source_context_type="receiving",
                source_context_id=order_key,
                is_archived=False,
                qty__gt=0,
            )
            .filter(
                models.Q(parent_container__container_code__in=pallet_codes)
                | models.Q(container__container_code__in=pallet_codes)
                | models.Q(container_code__in=pallet_codes)
            )
            .select_related("active_operation")
            .order_by("id")
        )
        grouped: dict[str, list[WarehouseStockSnapshot]] = {code.casefold(): [] for code in pallet_codes}
        for snapshot in snapshots:
            candidates = [
                str(getattr(snapshot.parent_container, "container_code", "") or "").strip(),
                str(getattr(snapshot.container, "container_code", "") or "").strip(),
                str(snapshot.container_code or "").strip(),
            ]
            for candidate in candidates:
                key = candidate.casefold()
                if key in grouped:
                    grouped[key].append(snapshot)
                    break

        result: dict[str, dict] = {}
        final_statuses = {WarehouseOperation.STATUS_DONE, WarehouseOperation.STATUS_CANCELED}
        for code in pallet_codes:
            key = code.casefold()
            if not sealed_by_code.get(key):
                continue
            pallet_snapshots = grouped.get(key) or []
            if not pallet_snapshots:
                if key in materialized_codes:
                    # Остаток по паллете уже создавался, а живых строк нет —
                    # товар разошелся. Звать ричтрак за ним не нужно.
                    result[code] = {
                        "status": "depleted",
                        "label": "Оприходована, товар разошелся",
                        "class": "placement-depleted",
                    }
                elif placement_permissions.get(key, False) or flow_closed:
                    result[code] = {"status": "ready", "label": "Готова к забору", "class": "ready-for-free-move"}
                else:
                    result[code] = {
                        "status": "locked",
                        "label": "Размещение недоступно",
                        "class": "placement-locked",
                    }
                continue
            active_move = any(
                snapshot.active_operation
                and snapshot.active_operation.operation_type == WarehouseOperation.TYPE_INTERNAL_RELOCATION
                and snapshot.active_operation.status not in final_statuses
                for snapshot in pallet_snapshots
            )
            placed = any(
                str(snapshot.zone_code or "").strip().upper() != "PR"
                or str(snapshot.warehouse_state_code or "").strip() != WarehouseStateCode.PLACED_IN_RECEIVING.value
                for snapshot in pallet_snapshots
            )
            if placed:
                result[code] = {"status": "placed", "label": "Размещена", "class": "free-move-done"}
            elif (placement_permissions.get(key, False) or flow_closed) and active_move:
                result[code] = {"status": "moving", "label": "В пути", "class": "ready-for-free-move"}
            elif placement_permissions.get(key, False) or flow_closed:
                result[code] = {"status": "ready", "label": "Готова к забору", "class": "ready-for-free-move"}
            else:
                result[code] = {
                    "status": "locked",
                    "label": "Размещение недоступно",
                    "class": "placement-locked",
                }
        return result

    @classmethod
    def build_receiving_flow_actual_pallet_locations(cls, order_id: str, pallets) -> dict[str, str]:
        order_key = str(order_id or "").strip()
        source_pallets = pallets if isinstance(pallets, list) else []
        pallet_codes = [
            str(pallet.get("code") or "").strip()
            for pallet in source_pallets
            if isinstance(pallet, dict) and str(pallet.get("code") or "").strip()
        ]
        if not order_key or not pallet_codes:
            return {}

        snapshots = (
            WarehouseStockSnapshot.objects.filter(
                source_context_type="receiving",
                source_context_id=order_key,
                is_archived=False,
                qty__gt=0,
            )
            .filter(
                models.Q(parent_container__container_code__in=pallet_codes)
                | models.Q(container__container_code__in=pallet_codes)
                | models.Q(container_code__in=pallet_codes)
            )
            .select_related("location", "container", "parent_container")
            .order_by("id")
        )
        labels_by_code: dict[str, list[str]] = {code.casefold(): [] for code in pallet_codes}
        for snapshot in snapshots:
            candidates = [
                str(getattr(snapshot.parent_container, "container_code", "") or "").strip(),
                str(getattr(snapshot.container, "container_code", "") or "").strip(),
                str(snapshot.container_code or "").strip(),
            ]
            pallet_key = next(
                (candidate.casefold() for candidate in candidates if candidate.casefold() in labels_by_code),
                "",
            )
            if not pallet_key:
                continue
            location = snapshot.location
            label = ""
            if location:
                zone_code = str(location.zone_code or snapshot.zone_code or "").strip().upper()
                if zone_code == "OS":
                    label = os_location_code(
                        row=int(location.row_no or 0),
                        section=int(location.section_no or 0),
                        tier=int(location.tier_no or 0),
                        cell=int(location.cell_no or 0),
                    )
                elif zone_code == "MR" and int(location.row_no or 0) > 0:
                    label = f"MR-{int(location.row_no)}"
                elif zone_code == "PR":
                    label = str(
                        location.display_name
                        or location.location_code
                        or zone_code
                    ).strip()
                else:
                    label = zone_code or str(location.display_name or location.location_code or "").strip()
            if not label:
                label = str(snapshot.zone_code or "").strip().upper()
            if label and label not in labels_by_code[pallet_key]:
                labels_by_code[pallet_key].append(label)

        result: dict[str, str] = {}
        for code in pallet_codes:
            labels = labels_by_code.get(code.casefold()) or []
            if labels:
                result[code] = " / ".join(labels)
        return result

    @classmethod
    def build_receiving_warehouse_move_panel(
        cls,
        *,
        order_id: str,
        placement_pallets,
        receiving_result,
        flow_locked: bool,
        role_allowed: bool,
        agency_id: int | None = None,
        row_sections: dict | None = None,
        tiers: list | tuple | None = None,
        cells_per_tier: int = 0,
    ) -> ReceivingWarehouseMovePanelResult:
        progress = cls.build_receiving_warehouse_move_progress(order_id, placement_pallets)
        rows = cls.build_receiving_warehouse_move_rows(
            order_id,
            placement_pallets,
            agency_id=agency_id,
            row_sections=row_sections,
            tiers=tiers,
            cells_per_tier=cells_per_tier,
        )
        can_send = WarehouseActionPolicy.can_send_receiving_to_storage(
            receiving_result,
            flow_closed=flow_locked,
            role_allowed=role_allowed,
            not_created_count=int(progress.get("not_created_count") or 0),
        ).allowed
        return ReceivingWarehouseMovePanelResult(
            progress=progress,
            rows=rows,
            can_send=can_send,
        )

    @classmethod
    def send_receiving_to_storage(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
        destinations_raw=None,
        draft_token: str = "",
    ) -> ReceivingWarehouseMoveResult:
        order_key = str(order_id or "").strip()
        if not order_key or not entries:
            return ReceivingWarehouseMoveResult(status="missing")
        status_entry = cls._current_status_entry(entries)
        latest = entries[-1]
        command_result = WarehouseCommandService.send_receiving_to_storage(
            order_id=order_key,
            agency=latest.agency if latest else None,
            role=role or "",
            status_payload=(status_entry.payload or {}) if status_entry else {},
            flow_closed=cls._flow_closed(entries),
            not_created_count=1,
        )
        if command_result.status == "denied":
            return ReceivingWarehouseMoveResult(
                status="not_ready",
                reason=command_result.reason,
                source_facts=list(command_result.source_facts or []),
            )
        destinations_by_pallet, destination_error = cls.parse_receiving_warehouse_destinations(destinations_raw)
        if destination_error:
            return ReceivingWarehouseMoveResult(
                status="invalid_destination",
                error_message=destination_error,
                source_facts=list(command_result.source_facts or []),
            )
        move_result = cls.create_receiving_warehouse_moves(
            order_id=order_key,
            entries=entries,
            role=role or "",
            user=user,
            destinations_by_pallet=destinations_by_pallet,
            draft_token=str(draft_token or "").strip(),
        )
        normalized_draft_token = str(draft_token or "").strip()
        if normalized_draft_token:
            PutawayDraftReservationService.release(draft_token=normalized_draft_token)
        if move_result.created_count > 0:
            move_result.status = "ok"
            return move_result
        if (
            move_result.skipped_missing_destination_count > 0
            and move_result.skipped_existing_count <= 0
        ):
            move_result.status = "missing_destination"
            return move_result
        if move_result.total_count > 0 and move_result.skipped_existing_count >= move_result.total_count:
            move_result.status = "exists"
            return move_result
        if move_result.skipped_existing_count > 0 or move_result.skipped_missing_destination_count > 0:
            move_result.status = "blocked"
            return move_result
        move_result.status = "none"
        return move_result

    @classmethod
    def _find_act_entry(cls, entries, act_type: str, label_hint: str):
        for entry in reversed(entries or []):
            if (entry.payload or {}).get("act") == act_type:
                return entry
        for entry in reversed(entries or []):
            label = ((entry.payload or {}).get("act_label") or "").lower()
            if label_hint in label:
                return entry
        return None

    @classmethod
    def _find_document_act_entry(cls, entries, act_type: str, label_hint: str):
        normalized_hint = str(label_hint or "").lower()
        candidates = []
        for index, entry in enumerate(entries or []):
            payload = entry.payload or {}
            if payload.get("act") != act_type:
                continue
            act_label = str(payload.get("act_label") or "").strip().lower()
            description = str(getattr(entry, "description", "") or "").strip().lower()
            has_label = bool(normalized_hint and normalized_hint in act_label)
            has_description = bool(normalized_hint and normalized_hint in description)
            if act_type == "receiving":
                has_document_rows = bool(payload.get("act_items") or payload.get("act_units"))
            elif act_type == "placement":
                has_document_rows = bool(payload.get("act_items"))
            else:
                has_document_rows = bool(payload)
            candidates.append(
                (
                    (
                        1 if has_label else 0,
                        1 if has_description or has_document_rows else 0,
                        index,
                    ),
                    entry,
                )
            )
        if candidates:
            preferred = [candidate for candidate in candidates if candidate[0][0] or candidate[0][1]]
            return max(preferred or candidates, key=lambda candidate: candidate[0])[1]
        return cls._find_act_entry(entries, act_type, label_hint)

    @staticmethod
    def _is_done_status(entry) -> bool:
        if not entry:
            return False
        payload = entry.payload or {}
        status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
        status_label = (payload.get("status_label") or "").lower()
        if status_value in {"done", "completed", "closed", "finished"}:
            return True
        return "выполн" in status_label

    @staticmethod
    def _act_storekeeper_signed_from_payload(payload: dict) -> bool:
        return bool((payload or {}).get("act_storekeeper_signed"))

    @staticmethod
    def _act_manager_signed_from_payload(payload: dict) -> bool:
        return bool((payload or {}).get("act_manager_signed"))

    @classmethod
    def _act_storekeeper_signed(cls, entries) -> bool:
        act_entry = cls._find_act_entry(entries, "receiving", "акт приемки")
        if not act_entry:
            return False
        return cls._act_storekeeper_signed_from_payload(act_entry.payload or {})

    @classmethod
    def _placement_closed(cls, entries) -> bool:
        placement_entry = cls._find_act_entry(entries, "placement", "акт размещения")
        if not placement_entry:
            return False
        state = ((placement_entry.payload or {}).get("act_state") or "closed").lower()
        return state == "closed"

    @classmethod
    def _create_manager_followup_task(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        user=None,
        submitted_at=None,
        observer=None,
    ) -> bool:
        if not agency:
            return False
        manager = (
            Employee.objects.filter(role="manager", is_active=True)
            .order_by("full_name")
            .first()
        )
        if not manager:
            return False
        title = f"Проверьте размещение по заявке на приемку товара №{order_id}"
        existing = Task.objects.filter(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=manager,
            title=title,
        ).exclude(status="done")
        if existing.exists():
            return False
        due_at = submitted_at if submitted_at is not None else timezone.localtime()
        description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
        Task.objects.create(
            title=title,
            description=description,
            route=f"/orders/receiving/{order_id}/",
            assigned_to=manager,
            observer=observer,
            created_by=cls._authenticated_user(user),
            due_date=_manager_due_date(due_at),
        )
        return True

    @classmethod
    def _create_manager_review_task(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        user=None,
        submitted_at=None,
    ) -> bool:
        if not agency:
            return False
        manager = (
            Employee.objects.filter(role="manager", is_active=True)
            .order_by("full_name")
            .first()
        )
        if not manager:
            return False
        due_at = submitted_at if submitted_at is not None else timezone.localtime()
        description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
        Task.objects.create(
            title=f"Подтвердите заявку на приемку товара №{order_id}",
            description=description,
            route=f"/orders/receiving/{order_id}/",
            assigned_to=manager,
            created_by=cls._authenticated_user(user),
            due_date=_manager_due_date(due_at),
        )
        return True

    @classmethod
    def _create_storekeeper_task(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        user=None,
        submitted_at=None,
        observer=None,
        payload: dict | None = None,
    ) -> bool:
        if not agency:
            return False
        storekeeper = (
            Employee.objects.filter(role="storekeeper", is_active=True)
            .order_by("full_name")
            .first()
        )
        if not storekeeper:
            return False
        due_at = submitted_at if submitted_at is not None else timezone.localtime()
        description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
        chz_label = cls.receiving_chz_service_label(
            (payload or {}).get("receiving_chz_service")
        )
        if chz_label:
            description = f"{description}\nЧестный знак: {chz_label}"
        Task.objects.create(
            title=f"Принять заявку на приемку товара №{order_id}",
            description=description,
            route=f"/orders/receiving/{order_id}/",
            assigned_to=storekeeper,
            observer=observer,
            created_by=cls._authenticated_user(user),
            due_date=due_at + timedelta(days=1),
        )
        return True

    @classmethod
    def submit_receiving_for_review(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        user=None,
        submitted_at=None,
    ) -> ReceivingDispatchResult:
        created = cls._create_manager_review_task(
            order_id=str(order_id or ""),
            agency=agency,
            user=user,
            submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
        )
        return ReceivingDispatchResult(
            payload={},
            manager_task_created=created,
        )

    @classmethod
    def start_receiving_work(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
    ) -> ReceivingActionResult:
        if not order_id or not entries:
            return ReceivingActionResult(status="missing")
        employee = cls._resolve_storekeeper(user)
        if not employee:
            return ReceivingActionResult(status="storekeeper_not_found")
        with transaction.atomic():
            WarehouseCommandService.acquire_receiving_order_lock(str(order_id or ""))
            locked_entries = list(
                OrderAuditEntry.objects.select_for_update()
                .filter(order_id=order_id, order_type="receiving")
                .order_by("created_at", "id")
            )
            if not locked_entries:
                return ReceivingActionResult(status="missing")
            access = cls.receiving_work_access(entries=locked_entries, user=user)
            if access.status != "allowed":
                return access
            owner = cls._receiving_work_owner(locked_entries)
            if owner:
                cls._assign_receiving_task_to_storekeeper(str(order_id), employee)
                return ReceivingActionResult(
                    status="already_in_progress",
                    payload=owner,
                    reason="receiving_work_owned",
                )
            status_entry = cls._current_status_entry(locked_entries)
            latest = locked_entries[-1]
            status_payload = (status_entry.payload or {}) if status_entry else {}
            command_result = WarehouseCommandService.start_receiving_flow(
                order_id=str(order_id or ""),
                agency=latest.agency if latest else None,
                role=role or "",
                status_payload=status_payload,
                flow_closed=cls._flow_closed(locked_entries),
            )
            if command_result.status != "started":
                return ReceivingActionResult(
                    status=command_result.status,
                    payload=dict(command_result.payload_update or {}),
                    reason=command_result.reason,
                    meta=dict(command_result.meta or {}),
                )
            payload = dict(status_payload)
            for key in (
                "act",
                "act_state",
                "act_items",
                "act_units",
                "act_boxes",
                "act_pallets",
                "flow_closed",
                "flow_closed_at",
                "flow_state",
                "flow_boxes",
                "flow_pallets",
                "flow_active_box",
                "flow_active_pallet",
            ):
                payload.pop(key, None)
            payload.update(command_result.payload_update)
            payload["storekeeper_employee_id"] = employee.id
            payload["storekeeper_name"] = employee.full_name or ""
            log_order_action(
                "status",
                order_id=order_id,
                order_type="receiving",
                user=cls._authenticated_user(user),
                agency=latest.agency if latest else None,
                description="Заявка взята в работу кладовщиком",
                payload=payload,
            )
            cls._assign_receiving_task_to_storekeeper(
                str(order_id),
                employee,
                started_at=timezone.localtime(),
            )
            return ReceivingActionResult(
                status="started",
                payload=payload,
            )

    @classmethod
    def take_over_receiving_work(
        cls,
        *,
        order_id: str,
        role: str,
        user=None,
    ) -> ReceivingActionResult:
        """Transfer an open receiving flow to the current storekeeper without resetting its draft."""
        order_key = str(order_id or "").strip()
        if not order_key:
            return ReceivingActionResult(status="missing")
        if str(role or "").strip().lower() != "storekeeper":
            return ReceivingActionResult(status="forbidden", reason="role_forbidden")
        employee = cls._resolve_storekeeper(user)
        if not employee:
            return ReceivingActionResult(status="storekeeper_not_found")

        with transaction.atomic():
            WarehouseCommandService.acquire_receiving_order_lock(order_key)
            locked_entries = list(
                OrderAuditEntry.objects.select_for_update()
                .filter(order_id=order_key, order_type="receiving")
                .order_by("created_at", "id")
            )
            if not locked_entries:
                return ReceivingActionResult(status="missing")
            pending = cls.warehouse_invalid_review_payload(locked_entries)
            if pending:
                return ReceivingActionResult(
                    status="warehouse_review_pending",
                    payload=pending,
                    reason="warehouse_invalid_review_pending",
                )
            if cls._flow_closed(locked_entries):
                return ReceivingActionResult(status="flow_closed", reason="flow_closed")

            owner = cls._receiving_work_owner(locked_entries)
            if not owner:
                return ReceivingActionResult(status="not_in_progress", reason="receiving_work_not_owned")
            try:
                previous_employee_id = int(owner.get("storekeeper_employee_id") or 0)
            except (TypeError, ValueError):
                previous_employee_id = 0
            previous_employee_name = str(owner.get("storekeeper_name") or "другого кладовщика").strip()
            status_entry = cls._current_status_entry(locked_entries)
            payload = dict(status_entry.payload or {}) if status_entry else {}
            if previous_employee_id == employee.id:
                cls._assign_receiving_task_to_storekeeper(order_key, employee)
                return ReceivingActionResult(
                    status="already_owned",
                    payload=payload,
                    meta={"storekeeper_employee_id": employee.id},
                )

            taken_over_at = timezone.localtime()
            payload.update(
                {
                    "status": "warehouse",
                    "status_label": "Взята в работу",
                    "storekeeper_employee_id": employee.id,
                    "storekeeper_name": employee.full_name or "",
                    "receiving_work_taken_over": True,
                    "receiving_work_taken_over_at": taken_over_at.isoformat(),
                    "receiving_work_taken_over_from_employee_id": previous_employee_id or None,
                    "receiving_work_taken_over_from_name": previous_employee_name,
                    "receiving_work_taken_over_to_employee_id": employee.id,
                    "receiving_work_taken_over_to_name": employee.full_name or "",
                }
            )
            reassigned_tasks = cls._assign_receiving_task_to_storekeeper(order_key, employee)
            latest = locked_entries[-1]
            new_employee_name = employee.full_name or str(employee)
            log_order_action(
                "status",
                order_id=order_key,
                order_type="receiving",
                user=cls._authenticated_user(user),
                agency=latest.agency,
                description=(
                    f"Кладовщик {new_employee_name} забрал заявку в работу у "
                    f"{previous_employee_name} и продолжил приемку"
                ),
                payload=payload,
            )
            return ReceivingActionResult(
                status="taken_over",
                payload=payload,
                meta={
                    "previous_storekeeper_employee_id": previous_employee_id or None,
                    "previous_storekeeper_name": previous_employee_name,
                    "storekeeper_employee_id": employee.id,
                    "storekeeper_name": new_employee_name,
                    "reassigned_tasks": reassigned_tasks,
                },
            )

    @classmethod
    def configure_receiving_act(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        goods_type: str,
        receiving_mode: str,
        shipping_return_order_number: str = "",
        user=None,
    ) -> ReceivingStatusUpdateResult:
        normalized_goods_type = str(goods_type or "").strip().lower()
        normalized_mode = str(receiving_mode or "").strip().lower()
        if role != "storekeeper" or normalized_goods_type not in cls.RECEIVING_GOODS_TYPE_LABELS:
            return ReceivingStatusUpdateResult(applied=False, payload={})
        order_key = str(order_id or "").strip()
        if not order_key:
            return ReceivingStatusUpdateResult(applied=False, payload={})

        with transaction.atomic():
            WarehouseCommandService.acquire_receiving_order_lock(order_key)
            locked_entries = list(
                OrderAuditEntry.objects.select_for_update()
                .filter(order_id=order_key, order_type="receiving")
                .order_by("created_at", "id")
            )
            if not locked_entries:
                return ReceivingStatusUpdateResult(applied=False, payload={})
            entries = locked_entries
            access = cls.receiving_work_access(entries=entries, user=user)
            if access.status != "allowed":
                return ReceivingStatusUpdateResult(applied=False, payload={})

            status_entry = cls._current_status_entry(entries)
            payload = dict(status_entry.payload or {}) if status_entry else {}
            locked_configuration = cls.receiving_configuration_lock(entries)
            locked_goods_type = str(
                locked_configuration.get("goods_type") or ""
            ).strip().lower()
            locked_mode = str(
                locked_configuration.get("receiving_mode") or ""
            ).strip().lower()
            if locked_goods_type and locked_goods_type != normalized_goods_type:
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload=payload,
                    description=(
                        "Тип приемки зафиксирован при начале работы и не может быть изменен."
                    ),
                )
            route_payload = dict(payload)
            route_payload["receiving_route"] = cls.receiving_route_from_entries(entries)
            route_constraint_error = cls.receiving_route_constraint_error(
                payload=route_payload,
                goods_type=normalized_goods_type,
            )
            if route_constraint_error:
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload=payload,
                    description=route_constraint_error,
                )
            mode_constraint_error = cls.receiving_mode_constraint_error(
                payload=payload,
                goods_type=normalized_goods_type,
                receiving_mode=normalized_mode,
            )
            if mode_constraint_error:
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload=payload,
                    description=mode_constraint_error,
                )
            normalized_mode = cls._normalize_receiving_mode_for_goods_type(
                normalized_mode,
                normalized_goods_type,
            )
            if locked_mode and locked_mode != normalized_mode:
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload=payload,
                    description=(
                        "Режим приемки зафиксирован при начале работы и не может быть изменен."
                    ),
                )
            existing_type = (payload.get("goods_type") or "").strip().lower()
            payload_items = [dict(item) for item in (payload.get("items") or []) if isinstance(item, dict)]
            source_order = None
            source_link_changed = False
            if normalized_goods_type == SHIPPING_RETURN_GOODS_TYPE:
                requested_source_number = (
                    str(shipping_return_order_number or "").strip()
                    or str(payload.get("shipping_return_order_number") or "").strip()
                )
                source_order = resolve_shipping_return_order(
                    agency=entries[-1].agency if entries else None,
                    number=requested_source_number,
                )
                if source_order is None:
                    return ReceivingStatusUpdateResult(
                        applied=False,
                        payload=payload,
                        description="Выберите отгруженную заявку этого клиента.",
                    )
                source_items = shipping_return_source_items(
                    source_order,
                    exclude_receiving_order_id=order_key,
                )
                if not source_items:
                    return ReceivingStatusUpdateResult(
                        applied=False,
                        payload=payload,
                        description="По этой отгрузке нет товара, доступного для возврата.",
                    )
                source_link_changed = (
                    str(payload.get("shipping_return_order_number") or "").strip()
                    != source_order.number
                )
                payload_items = source_items
                payload["shipping_return_order_id"] = int(source_order.id)
                payload["shipping_return_order_number"] = source_order.number
                payload["shipping_return_order_status"] = source_order.status
                payload["shipping_return_source_total_qty"] = int(
                    source_order.items.aggregate(total=models.Sum("qty_shipped")).get("total") or 0
                )
                payload["shipping_return_available_qty"] = sum(
                    int(item.get("qty") or 0) for item in source_items
                )
                payload["items"] = payload_items
            elif existing_type == SHIPPING_RETURN_GOODS_TYPE:
                for key in (
                    "shipping_return_order_id",
                    "shipping_return_order_number",
                    "shipping_return_order_status",
                    "shipping_return_source_total_qty",
                    "shipping_return_available_qty",
                ):
                    payload.pop(key, None)
            marked_items_map = cls._receiving_marked_items_map(
                entries[-1].agency_id if entries else None,
                payload_items,
            )
            if normalized_mode == "cz" and not marked_items_map and payload_items:
                normalized_mode = "standard"
            payload_changed = False
            description = ""
            if existing_type != normalized_goods_type:
                payload["goods_type"] = normalized_goods_type
                payload["goods_type_label"] = cls.RECEIVING_GOODS_TYPE_LABELS[normalized_goods_type]
                payload_changed = True
                description = f"Тип товара: {cls.RECEIVING_GOODS_TYPE_LABELS[normalized_goods_type]}"
                for item in payload_items:
                    item_type = cls._normalize_flow_goods_type(item.get("goods_type"))
                    if normalized_goods_type == SHIPPING_RETURN_GOODS_TYPE:
                        item["goods_type"] = SHIPPING_RETURN_GOODS_TYPE
                    elif existing_type and item_type == existing_type:
                        item["goods_type"] = normalized_goods_type
                migration = cls._migrate_receiving_flow_draft_goods_type(
                    entries=entries,
                    order_id=order_key,
                    previous_goods_type=existing_type,
                    next_goods_type=normalized_goods_type,
                )
                if migration.get("draft_found"):
                    payload["flow_goods_type_change"] = {
                        "from": existing_type,
                        "to": normalized_goods_type,
                        "boxes_updated": migration.get("boxes_updated", 0),
                        "pallets_updated": migration.get("pallets_updated", 0),
                        "box_codes_updated": migration.get("box_codes_updated", 0),
                        "pallet_codes_updated": migration.get("pallet_codes_updated", 0),
                        "marking_codes_updated": migration.get("marking_codes_updated", 0),
                        "flow_client_version": migration.get("flow_client_version", 0),
                    }
            if source_link_changed:
                payload_changed = True
                description = (
                    f"Тип товара: {SHIPPING_RETURN_GOODS_TYPE_LABEL} · "
                    f"{format_order_number('shipping', source_order.number)}"
                )
            if normalized_mode in cls.RECEIVING_MODES and payload.get("receiving_mode") != normalized_mode:
                payload["receiving_mode"] = normalized_mode
                payload_changed = True
                if existing_type:
                    description = "Обновлен режим приемки"
            if cls._receiving_has_started(entries) and not locked_configuration.get("explicit"):
                payload.update(
                    {
                        "receiving_configuration_locked": True,
                        "receiving_configuration_locked_goods_type": (
                            locked_goods_type or normalized_goods_type
                        ),
                        "receiving_configuration_locked_mode": locked_mode or normalized_mode,
                        "receiving_configuration_locked_at": timezone.localtime().isoformat(),
                    }
                )
                payload_changed = True
                if not description:
                    description = "Настройки приемки зафиксированы"
            if payload_items:
                cls._attach_receiving_nomenclature_metadata(
                    agency=entries[-1].agency if entries else None,
                    items=payload_items,
                    goods_type=normalized_goods_type,
                    order_id=order_key,
                    order_type="receiving",
                )
                if payload_items != (payload.get("items") or []):
                    payload["items"] = payload_items
                    payload_changed = True
                    if not description:
                        description = "Обновлена номенклатура приемки"
            if not payload_changed:
                return ReceivingStatusUpdateResult(
                    applied=True,
                    payload=payload,
                    description="Настройки приемки без изменений",
                )
            latest = entries[-1] if entries else None
            log_order_action(
                "status",
                order_id=order_key,
                order_type="receiving",
                user=cls._authenticated_user(user),
                agency=latest.agency if latest else None,
                description=description,
                payload=payload,
            )
            if source_link_changed and source_order is not None:
                log_order_action(
                    "update",
                    order_id=source_order.number,
                    order_type="shipping",
                    user=cls._authenticated_user(user),
                    agency=source_order.agency,
                    description=(
                        "Создан возврат с отгрузки: приемка "
                        f"{format_order_number('receiving', order_key)}"
                    ),
                    payload={
                        "shipping_return_receiving_order_id": order_key,
                        "shipping_return_receiving_order_number": format_order_number(
                            "receiving", order_key
                        ),
                        "shipping_return_order_number": source_order.number,
                        "shipping_return_status": "linked",
                    },
                )
            return ReceivingStatusUpdateResult(
                applied=True,
                payload=payload,
                description=description,
            )

    @classmethod
    def set_receiving_marking_mode(
        cls,
        *,
        order_id: str,
        role: str,
        receiving_mode: str,
        user=None,
    ) -> ReceivingStatusUpdateResult:
        """Change the marking mode before the first accepted unit."""
        order_key = str(order_id or "").strip()
        normalized_mode = str(receiving_mode or "").strip().lower()
        if role != "storekeeper" or not order_key or normalized_mode not in cls.RECEIVING_MODES:
            return ReceivingStatusUpdateResult(
                applied=False,
                payload={},
                description="Некорректный режим приемки.",
            )

        with transaction.atomic():
            WarehouseCommandService.acquire_receiving_order_lock(order_key)
            entries = list(
                OrderAuditEntry.objects.select_for_update()
                .filter(order_id=order_key, order_type="receiving")
                .order_by("created_at", "id")
            )
            if not entries:
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload={},
                    description="Заявка не найдена.",
                )
            status_entry = cls._current_status_entry(entries)
            payload = dict(status_entry.payload or {}) if status_entry else {}
            if cls._flow_closed(entries):
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload=payload,
                    description="Приемка уже завершена.",
                )
            access = cls.receiving_work_access(entries=entries, user=user)
            if access.status != "allowed":
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload=payload,
                    description="Заявка закреплена за другим кладовщиком.",
                )
            if not cls._receiving_work_owner(entries):
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload=payload,
                    description="Сначала возьмите заявку в работу.",
                )
            locked_configuration = cls.receiving_configuration_lock(entries)
            locked_mode = str(
                locked_configuration.get("receiving_mode") or ""
            ).strip().lower()
            if locked_mode and locked_mode != normalized_mode:
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload=payload,
                    description=(
                        "Режим приемки зафиксирован при начале работы и не может быть изменен."
                    ),
                )
            flow_state = cls.find_receiving_flow_state(entries)
            has_accepted_units = cls.receiving_flow_has_items(flow_state) or MarkingCode.objects.filter(
                order_type="receiving",
                order_id=order_key,
                used_at__isnull=False,
            ).exists()
            if has_accepted_units:
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload=payload,
                    description="Режим Честного знака нельзя менять после первого принятого товара.",
                )
            goods_type = str(payload.get("goods_type") or "").strip().lower()
            if normalized_mode == "cz" and goods_type == "no":
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload=payload,
                    description="Для типа «Не обработанный» сканирование Честного знака недоступно.",
                )
            constraint_error = cls.receiving_mode_constraint_error(
                payload=payload,
                goods_type=goods_type,
                receiving_mode=normalized_mode,
            )
            if constraint_error:
                return ReceivingStatusUpdateResult(
                    applied=False,
                    payload=payload,
                    description=constraint_error,
                )
            if cls.effective_receiving_mode(payload) == normalized_mode:
                return ReceivingStatusUpdateResult(
                    applied=True,
                    payload=payload,
                    description="Режим приемки без изменений.",
                )
            payload["receiving_mode"] = normalized_mode
            payload["receiving_mode_label"] = (
                "Приемка с ЧЗ" if normalized_mode == "cz" else "Обычная приемка"
            )
            latest = entries[-1]
            log_order_action(
                "status",
                order_id=order_key,
                order_type="receiving",
                user=cls._authenticated_user(user),
                agency=latest.agency,
                description=(
                    "Приемщик включил сканирование Честного знака"
                    if normalized_mode == "cz"
                    else "Приемщик отключил сканирование Честного знака"
                ),
                payload=payload,
            )
            return ReceivingStatusUpdateResult(
                applied=True,
                payload=payload,
                description=payload["receiving_mode_label"],
            )

    @classmethod
    def reopen_receiving_flow(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
    ) -> ReceivingActionResult:
        if not order_id or not entries:
            return ReceivingActionResult(status="missing")
        latest = entries[-1] if entries else None
        closed_entry = next(
            (entry for entry in reversed(entries) if (entry.payload or {}).get("flow_closed")),
            None,
        )
        closed_payload = closed_entry.payload if closed_entry else {}
        command_result = WarehouseCommandService.reopen_receiving_flow(
            order_id=str(order_id or ""),
            agency=latest.agency if latest else None,
            role=role or "",
            status_payload=(closed_payload if isinstance(closed_payload, dict) else {}),
            flow_closed=cls._flow_closed(entries),
            flow_closed_at=(closed_payload or {}).get("flow_closed_at"),
        )
        if command_result.status != "reopened":
            return ReceivingActionResult(
                status=command_result.status,
                payload=dict(command_result.payload_update or {}),
                reason=command_result.reason,
                meta=dict(command_result.meta or {}),
            )
        actor = cls._authenticated_user(user)
        log_staff_overaction(
            "update",
            user=actor,
            agency=latest.agency if latest else None,
            description=f"Избыточное действие: повторное открытие приемки потоком (заявка {order_id})",
            snapshot=command_result.meta,
        )
        log_order_action(
            "update",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=latest.agency if latest else None,
            description="Повторное открытие приемки потоком",
            payload=command_result.payload_update,
        )
        return ReceivingActionResult(
            status="reopened",
            payload=dict(command_result.payload_update or {}),
            meta=dict(command_result.meta or {}),
        )

    @classmethod
    def submit_receiving_order(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        payload: dict,
        submit_action: str,
        user=None,
        submitted_at=None,
        existing_order_id: str = "",
        old_payload: dict | None = None,
        dispatch_review: bool = False,
        is_autosave: bool = False,
    ) -> ReceivingSubmissionResult:
        chz_service = cls.normalize_receiving_chz_service(
            (payload or {}).get("receiving_chz_service")
        )
        route_raw = str((payload or {}).get("receiving_route") or "").strip()
        receiving_route = cls.normalize_receiving_route(route_raw)
        if route_raw and not receiving_route:
            raise ValueError("Неизвестный маршрут после приемки.")
        if not receiving_route:
            receiving_route = cls.normalize_receiving_route(
                (old_payload or {}).get("receiving_route"),
                default="fbo",
            )
        payload["receiving_route"] = receiving_route
        if submit_action != "draft" and not chz_service:
            raise ValueError("Укажите, требуется ли сканирование Честного знака.")
        actor = cls._authenticated_user(user)
        order_key = str(existing_order_id or order_id or "").strip()
        status_value = str((payload or {}).get("status") or "").strip()
        status_label = str((payload or {}).get("status_label") or "").strip()
        review_dispatched = False
        if existing_order_id and bool(
            (old_payload or {}).get("receiving_concrete_location_required")
        ):
            payload["receiving_concrete_location_required"] = True
            payload["concrete_location_version"] = 1
        if existing_order_id:
            existing_entries_for_route = list(
                OrderAuditEntry.objects.filter(
                    order_id=order_key,
                    order_type="receiving",
                ).order_by("created_at", "id")
            )
            route_is_locked = bool(
                cls.receiving_configuration_lock(existing_entries_for_route)
                or cls._receiving_work_owner(existing_entries_for_route)
                or cls.receiving_flow_has_items(
                    cls.find_receiving_flow_state(existing_entries_for_route)
                )
            )
            previous_route = cls.receiving_route_from_entries(existing_entries_for_route)
            if route_is_locked and receiving_route != previous_route:
                raise ValueError(
                    "Маршрут FBS/FBO зафиксирован после начала приемки и не может быть изменен."
                )
        previous_route_for_eligibility = cls.normalize_receiving_route(
            (old_payload or {}).get("receiving_route"),
            default="fbo",
        )
        if receiving_route == "fbs" and (
            not existing_order_id or previous_route_for_eligibility != "fbs"
        ):
            from fbs.services.client_profiles import is_client_fbs_enabled

            if agency is None or not is_client_fbs_enabled(agency):
                raise ValueError("Маршрут FBS недоступен для этого клиента.")
        if existing_order_id and is_autosave and submit_action == "draft":
            existing_entries = (
                OrderAuditEntry.objects.filter(
                    order_id=order_key,
                    order_type="receiving",
                )
                .only("payload")
                .order_by("-created_at")
            )
            for entry in existing_entries:
                existing_payload = dict(entry.payload or {})
                existing_status = str(
                    existing_payload.get("status")
                    or existing_payload.get("submit_action")
                    or ""
                ).strip().lower()
                if not existing_status or existing_status == "draft":
                    continue
                return ReceivingSubmissionResult(
                    order_id=order_key,
                    payload=existing_payload,
                    status_value=existing_status,
                    status_label=str(existing_payload.get("status_label") or "").strip(),
                    was_update=True,
                    review_dispatched=False,
                )
        if existing_order_id:
            changes = _describe_payload_changes(old_payload or {}, payload or {})
            if changes:
                description = f"Исправление заявки №{order_key}: " + "; ".join(changes)
            else:
                description = f"Исправление заявки №{order_key}: без изменений"
            if submit_action == "send":
                description = f"Отправлено менеджеру. {description}"
            log_order_action(
                "update",
                order_id=order_key,
                order_type="receiving",
                user=actor,
                agency=agency,
                description=description,
                payload=payload,
            )
            if submit_action == "send" and dispatch_review:
                cls.submit_receiving_for_review(
                    order_id=order_key,
                    agency=agency,
                    user=actor,
                    submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
                )
                review_dispatched = True
            return ReceivingSubmissionResult(
                order_id=order_key,
                payload=dict(payload or {}),
                status_value=status_value,
                status_label=status_label,
                was_update=True,
                review_dispatched=review_dispatched,
            )

        payload["receiving_concrete_location_required"] = True
        payload["concrete_location_version"] = 1
        action_label = "черновик" if submit_action == "draft" else "заявка"
        log_order_action(
            "create",
            order_id=order_key,
            order_type="receiving",
            user=actor,
            agency=agency,
            description=f"Заявка на приемку №{order_key} ({action_label})",
            payload=payload,
        )
        if submit_action != "draft" and dispatch_review:
            cls.submit_receiving_for_review(
                order_id=order_key,
                agency=agency,
                user=actor,
                submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
            )
            review_dispatched = True
        return ReceivingSubmissionResult(
            order_id=order_key,
            payload=dict(payload or {}),
            status_value=status_value,
            status_label=status_label,
            was_update=False,
            review_dispatched=review_dispatched,
        )

    @classmethod
    def send_receiving_act_to_client(
        cls,
        *,
        order_id: str,
        entries,
        user=None,
    ) -> ReceivingStatusUpdateResult:
        if not entries:
            return ReceivingStatusUpdateResult(applied=False, payload={})
        receiving_entry = cls._find_act_entry(entries, "receiving", "акт приемки")
        placement_entry = cls._find_act_entry(entries, "placement", "акт размещения")
        if not receiving_entry or not placement_entry:
            return ReceivingStatusUpdateResult(applied=False, payload={})
        if not cls._placement_closed(entries):
            return ReceivingStatusUpdateResult(applied=False, payload={})
        receiving_payload = receiving_entry.payload or {}
        if not cls._act_storekeeper_signed_from_payload(receiving_payload):
            return ReceivingStatusUpdateResult(applied=False, payload={})
        if not cls._act_manager_signed_from_payload(receiving_payload):
            return ReceivingStatusUpdateResult(applied=False, payload={})
        status_entry = cls._current_status_entry(entries)
        if cls._is_done_status(status_entry):
            return ReceivingStatusUpdateResult(applied=False, payload={})
        actor = cls._authenticated_user(user)
        latest = entries[-1]
        agency = next((entry.agency for entry in reversed(entries) if entry.agency), None) or latest.agency
        payload = dict(status_entry.payload or {}) if status_entry else {}
        payload["status"] = "done"
        payload["status_label"] = "Выполнена"
        act_label = (receiving_entry.payload or {}).get("act_label") or "Акт приемки"
        payload["act_sent"] = act_label
        payload["act_sent_at"] = timezone.localtime().isoformat()
        if "act_viewed" not in payload:
            payload["act_viewed"] = False
        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description="Акт отправлен клиенту",
            payload=payload,
        )
        log_order_action(
            "update",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description=f"{act_label} отправлен клиенту",
            payload={"message": act_label},
        )
        closed_count = cls._close_manager_tasks(str(order_id or ""))
        return ReceivingStatusUpdateResult(
            applied=True,
            payload=payload,
            description="Акт отправлен клиенту",
            manager_tasks_closed=closed_count,
        )

    @classmethod
    def open_receiving_placement(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
    ) -> ReceivingActionResult:
        if not order_id or not entries:
            return ReceivingActionResult(status="missing")
        placement_entry = cls._find_act_entry(entries, "placement", "акт размещения")
        if not placement_entry:
            return ReceivingActionResult(status="missing")
        payload = placement_entry.payload or {}
        current_state = (payload.get("act_state") or "closed").lower()
        status_entry = cls._current_status_entry(entries)
        command_result = WarehouseCommandService.open_receiving_placement(
            order_id=str(order_id or ""),
            agency=entries[-1].agency if entries else None,
            role=role or "",
            act_state=current_state,
            signed_by_storekeeper=cls._act_storekeeper_signed(entries),
            status_payload=(status_entry.payload or {}) if status_entry else {},
        )
        if command_result.status != "opened":
            return ReceivingActionResult(
                status=command_result.status,
                payload=dict(command_result.payload_update or {}),
                reason=command_result.reason,
                meta=dict(command_result.meta or {}),
            )
        act_payload = dict(payload)
        act_payload.update(command_result.payload_update)
        if payload.get("act_label") and not command_result.payload_update.get("act_label"):
            act_payload["act_label"] = payload.get("act_label")
        latest = entries[-1]
        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=cls._authenticated_user(user),
            agency=latest.agency,
            description="Открыт акт размещения",
            payload=act_payload,
        )
        return ReceivingActionResult(
            status="opened",
            payload=act_payload,
        )

    @classmethod
    def can_confirm_receiving_to_warehouse(cls, user) -> bool:
        actor = cls._authenticated_user(user)
        if actor is None or not getattr(actor, "is_active", False):
            return False
        if is_developer_login(actor):
            return True
        allowed = {"manager", "head_manager", "director", "admin", "developer"}
        return any(
            bool(get_employee_roles(employee).intersection(allowed))
            for employee in Employee.objects.filter(user=actor, is_active=True)
        )

    @classmethod
    def confirm_receiving_to_warehouse(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        status_payload: dict | None,
        user=None,
        submitted_at=None,
    ) -> ReceivingDispatchResult:
        if not cls.can_confirm_receiving_to_warehouse(user):
            raise PermissionError("Отправить приемку на склад может только менеджер")
        actor = cls._authenticated_user(user)
        observer = cls._resolve_observer(user)
        payload = dict(status_payload or {})
        payload["status"] = "warehouse"
        payload["status_label"] = "В ожидании поставки товара"
        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description="Подтверждено и отправлено на склад",
            payload=payload,
        )
        closed_count = cls._close_manager_tasks(str(order_id or ""))
        storekeeper_created = cls._create_storekeeper_task(
            order_id=str(order_id or ""),
            agency=agency,
            user=actor,
            submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
            observer=observer,
            payload=payload,
        )
        return ReceivingDispatchResult(
            payload=payload,
            manager_tasks_closed=closed_count,
            storekeeper_task_created=storekeeper_created,
        )

    @classmethod
    @transaction.atomic
    def complete_receiving_flow(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        status_payload: dict | None,
        has_mismatch: bool,
        receiving_mode: str,
        act_items: list[dict],
        placement_items: list[dict],
        boxes: list[dict],
        pallets: list[dict],
        flow_state: dict,
        act_units: list[dict] | None = None,
        eta_at: str = "",
        received_at: str = "",
        vehicle_number: str = "",
        has_closed_placement_act: bool = False,
        flow_client_version=None,
        receiving_location_code: str = "",
        user=None,
        submitted_at=None,
    ) -> ReceivingWorkflowResult:
        order_key = str(order_id or "").strip()
        WarehouseCommandService.acquire_receiving_order_lock(order_key)
        actor = cls._authenticated_user(user)
        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_key, order_type="receiving")
            .select_related("agency")
            .order_by("created_at", "id")
        )
        access = cls.receiving_work_access(entries=entries, user=user)
        if access.status != "allowed":
            owner_name = str(access.payload.get("storekeeper_name") or "другого кладовщика")
            raise PermissionError(f"Приёмка уже в работе у {owner_name}.")
        if cls._flow_closed(entries):
            act_payload = {}
            placement_payload = {}
            for entry in reversed(entries):
                payload = entry.payload if isinstance(entry.payload, dict) else {}
                if not act_payload and payload.get("act") == "receiving":
                    act_payload = dict(payload)
                if not placement_payload and payload.get("act") == "placement":
                    placement_payload = dict(payload)
                if act_payload and placement_payload:
                    break
            return ReceivingWorkflowResult(
                act_payload=act_payload,
                placement_payload=placement_payload,
                placement_previously_closed=bool(placement_payload),
                already_completed=True,
            )
        if flow_client_version is not None:
            submitted_flow_version = cls._parse_flow_client_version(flow_client_version)
            current_flow_version = cls._latest_flow_client_version(entries)
            if submitted_flow_version != current_flow_version:
                raise ValueError(
                    "Черновик приемки изменен в другой вкладке. "
                    "Обновите страницу перед завершением."
                )
            saved_flow_state = cls.find_receiving_flow_state(entries)
            saved_stock_state = dict(saved_flow_state or {})
            submitted_stock_state = dict(flow_state or {})
            for state in (saved_stock_state, submitted_stock_state):
                state.pop("activeBox", None)
                state.pop("activePallet", None)
                state.pop("received_at", None)
            if saved_stock_state and submitted_stock_state != saved_stock_state:
                raise ValueError(
                    "Состав приемки отличается от сохраненного черновика. "
                    "Обновите страницу перед завершением."
                )
            locked_pallet_changes = cls.receiving_flow_locked_pallet_changes(
                order_id=order_key,
                previous_flow_state=saved_flow_state,
                next_flow_state=flow_state,
            )
            if locked_pallet_changes:
                raise ValueError(
                    "Состав паллеты нельзя менять после передачи на размещение: "
                    + ", ".join(locked_pallet_changes)
                    + "."
                )
        observer = cls._resolve_observer(user)
        normalized_goods_type = str((status_payload or {}).get("goods_type") or "").strip().lower()
        route_payload = dict(status_payload or {})
        route_payload["receiving_route"] = cls.receiving_route_from_entries(entries)
        route_constraint_error = cls.receiving_route_constraint_error(
            payload=route_payload,
            goods_type=normalized_goods_type,
        )
        if route_constraint_error:
            raise ValueError(route_constraint_error)
        if route_payload["receiving_route"] == "fbs":
            from fbs.services.receiving_movements import receiving_fbs_route_rows

            routed_codes = {
                str(row.get("source_pallet_code") or "").strip().casefold()
                for row in receiving_fbs_route_rows(
                    agency_id=int(getattr(agency, "id", 0) or 0),
                    order_id=order_key,
                )
                if row.get("exact")
            }
            required_codes = {
                str(pallet.get("code") or "").strip().casefold()
                for pallet in pallets or []
                if isinstance(pallet, dict)
                and pallet.get("sealed")
                and str(pallet.get("code") or "").strip()
            }
            missing_codes = sorted(required_codes - routed_codes)
            if missing_codes:
                raise ValueError(
                    "Не назначено точное FBS-место для палет: "
                    + ", ".join(missing_codes)
                    + ". Выберите FBS-место для каждой закрытой паллеты и разрешите размещение."
                )
        source_order = None
        if normalized_goods_type == SHIPPING_RETURN_GOODS_TYPE:
            source_order = resolve_shipping_return_order(
                agency=agency,
                number=(status_payload or {}).get("shipping_return_order_number"),
                for_update=True,
            )
            if source_order is None:
                raise ValueError("Не найдена выбранная отгрузка для возврата.")
            new_mark_allowance: dict[tuple[str, str], int] = {}
            for unit in act_units or []:
                if not isinstance(unit, dict) or unit.get("source_mark_verified") is not False:
                    continue
                item_key = shipping_return_item_key(
                    unit.get("sku_code") or unit.get("sku"),
                    unit.get("size"),
                )
                new_mark_allowance[item_key] = new_mark_allowance.get(item_key, 0) + 1
            valid_return, return_reason, return_details = validate_shipping_return_items(
                source_order,
                act_items,
                exclude_receiving_order_id=order_key,
                allowed_overage_by_item=new_mark_allowance,
            )
            if not valid_return:
                if return_reason == "shipping_return_qty_exceeded":
                    raise ValueError(
                        "Количество возврата превышает остаток по отгрузке: "
                        f"{return_details.get('sku_code') or '-'} — "
                        f"можно {return_details.get('allowed_qty') or 0} шт."
                    )
                raise ValueError(
                    "В возврате есть товар, которого не было в выбранной отгрузке: "
                    f"{return_details.get('sku_code') or '-'}"
                )
        cls._attach_receiving_nomenclature_metadata(
            agency=agency,
            items=act_items,
            goods_type=normalized_goods_type,
            order_id=order_id,
            order_type="receiving",
        )
        cls._attach_receiving_nomenclature_metadata(
            agency=agency,
            items=placement_items,
            goods_type=normalized_goods_type,
            order_id=order_id,
            order_type="receiving",
        )
        for box in boxes or []:
            if not isinstance(box, dict):
                continue
            cls._attach_receiving_nomenclature_metadata(
                agency=agency,
                items=box.get("items") or [],
                goods_type=normalized_goods_type,
                order_id=order_id,
                order_type="receiving",
            )
        for pallet in pallets or []:
            if not isinstance(pallet, dict):
                continue
            cls._attach_receiving_nomenclature_metadata(
                agency=agency,
                items=pallet.get("items") or [],
                goods_type=normalized_goods_type,
                order_id=order_id,
                order_type="receiving",
            )
        command_result = WarehouseCommandService.complete_receiving_flow(
            order_id=order_id,
            agency=agency,
            status_payload=status_payload,
            has_mismatch=has_mismatch,
            receiving_mode=receiving_mode,
            act_items=act_items,
            placement_items=placement_items,
            boxes=boxes,
            pallets=pallets,
            flow_state=flow_state,
            act_units=act_units,
            eta_at=eta_at,
            received_at=received_at,
            vehicle_number=vehicle_number,
            has_closed_placement_act=has_closed_placement_act,
            receiving_location_code=receiving_location_code,
            concrete_location_required=cls.requires_concrete_receiving_location(entries),
            performed_by=actor,
        )
        act_payload = dict(command_result.payload_update)
        placement_payload = dict(command_result.meta.get("placement_payload") or {})
        placement_previously_closed = bool(command_result.meta.get("placement_previously_closed"))

        log_order_action(
            "update",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description="Обновлен акт размещения" if placement_previously_closed else "Создан акт размещения",
            payload=placement_payload,
        )
        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description="Создан акт приемки",
            payload=act_payload,
        )
        if source_order is not None:
            returned_qty = sum(int(item.get("actual_qty") or 0) for item in act_items or [])
            returned_mark_count = len(act_units or [])
            log_order_action(
                "status",
                order_id=source_order.number,
                order_type="shipping",
                user=actor,
                agency=source_order.agency,
                description=(
                    "Принят возврат с отгрузки: "
                    f"{format_order_number('receiving', order_key)} · "
                    f"{returned_qty} шт. · {len(boxes or [])} кор."
                ),
                payload={
                    "shipping_return_receiving_order_id": order_key,
                    "shipping_return_receiving_order_number": format_order_number(
                        "receiving", order_key
                    ),
                    "shipping_return_order_number": source_order.number,
                    "shipping_return_status": "completed",
                    "shipping_return_qty": returned_qty,
                    "shipping_return_box_count": len(boxes or []),
                    "shipping_return_pallet_count": len(pallets or []),
                    "shipping_return_mark_count": returned_mark_count,
                },
            )

        from billing.warehouse_services import sync_receiving_facts_to_billing

        sync_receiving_facts_to_billing(
            client=agency,
            order_id=order_key,
            completed_at=submitted_at if submitted_at is not None else timezone.now(),
            user=actor,
            source_payload={
                "goods_type": normalized_goods_type,
                "receiving_mode": receiving_mode,
                "items": list(act_items or []),
                "act_items": list(act_items or []),
                "act_boxes": list(boxes or []),
                "act_pallets": list(pallets or []),
                "box_count": len(boxes or []),
                "pallet_count": len(pallets or []),
                "received_at": received_at,
            },
        )

        closed_count = 0
        if not placement_previously_closed:
            closed_count = cls._close_storekeeper_tasks(str(order_id or ""))
        created_followup = cls._create_manager_followup_task(
            order_id=str(order_id or ""),
            agency=agency,
            user=actor,
            submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
            observer=observer,
        )
        return ReceivingWorkflowResult(
            act_payload=act_payload,
            placement_payload=placement_payload,
            placement_previously_closed=placement_previously_closed,
            storekeeper_tasks_closed=closed_count,
            manager_followup_created=created_followup,
        )

    @classmethod
    def close_receiving_placement(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        status_payload: dict | None,
        placement_items: list[dict],
        boxes: list[dict],
        pallets: list[dict],
        has_closed_act: bool = False,
        user=None,
        submitted_at=None,
    ) -> ReceivingWorkflowResult:
        actor = cls._authenticated_user(user)
        observer = cls._resolve_observer(user)
        command_result = WarehouseCommandService.close_receiving_placement(
            order_id=order_id,
            agency=agency,
            status_payload=status_payload,
            placement_items=placement_items,
            boxes=boxes,
            pallets=pallets,
            has_closed_act=has_closed_act,
            performed_by=actor,
        )
        act_payload = dict(command_result.payload_update)
        placement_previously_closed = bool(command_result.meta.get("placement_previously_closed"))

        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description="Обновлен акт размещения" if placement_previously_closed else "Создан акт размещения",
            payload=act_payload,
        )

        closed_count = 0
        if not placement_previously_closed:
            closed_count = cls._close_storekeeper_tasks(str(order_id or ""))
        created_followup = cls._create_manager_followup_task(
            order_id=str(order_id or ""),
            agency=agency,
            user=actor,
            submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
            observer=observer,
        )
        return ReceivingWorkflowResult(
            act_payload=act_payload,
            placement_payload=act_payload,
            placement_previously_closed=placement_previously_closed,
            storekeeper_tasks_closed=closed_count,
            manager_followup_created=created_followup,
        )
