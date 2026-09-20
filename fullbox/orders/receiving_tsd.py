from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from agent.models import DeviceAgent
from audit.models import AuditEntry, AuditJournal, OrderAuditEntry, log_order_action
from employees.access import get_request_role
from employees.models import Employee
from fbs.desktop_presence import get_desktop_presences
from labels.utils import agent_printer_names_from_meta, is_virtual_printer_name
from processing_app.models import ProcessingPrintJob
from processing_app.services import ProcessingWorkflowService
from reachtruck.services.putaway_planner import putaway_location_label, putaway_location_scan_code
from receiving_cz.services import (
    _find_item_by_barcode,
    accepted_units,
    issue_container_for_order,
    load_order_context,
    reconcile_flow_state_with_units,
    scan_unit,
    serialize_unit,
)
from sklad.services.operational_locations import require_receiving_location
from sklad.services.warehouse_commands import WarehouseCommandService
from sklad.services.warehouse_write_path import WarehouseWritePathService
from stockmap.views import _OS_CELLS_PER_TIER, _OS_ROW_SECTIONS, _OS_TIERS
from todo.models import Task

from .services import ReceivingWorkflowService
from .receiving_tsd_goods_type import claim_tsd_receiving, configure_tsd_goods_type, goods_type_choices


_RECEIVING_TASK_ROUTE_RE = re.compile(r"^/orders/receiving/([^/]+)/$")
_EVENT_JOURNAL_CODE = "receiving_tsd"
_EVENT_TYPES = {
    "claimed",
    "scan_ok",
    "scan_error",
    "box_opened",
    "box_closed",
    "pallet_closed",
    "location_selected",
    "completed",
    "print_queued",
    "print_failed",
}
_LOCATION_EVENT = "receiving_tsd_location_selected"
_ASSET_CONTENT_TYPES = {
    "receiving_tsd.css": "text/css; charset=utf-8",
    "receiving_tsd.js": "application/javascript; charset=utf-8",
}


class _TsdStateError(RuntimeError):
    def __init__(self, status: str, message: str, *, http_status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message
        self.http_status = http_status


def _storekeeper_employee(request) -> Employee | None:
    if not request.user.is_authenticated or get_request_role(request) != "storekeeper":
        return None
    return Employee.objects.filter(
        user=request.user,
        role="storekeeper",
        is_active=True,
    ).first()


def _deny_unless_storekeeper(request):
    employee = _storekeeper_employee(request)
    if employee is None:
        return None, HttpResponseForbidden("Доступ разрешен только кладовщику.")
    return employee, None


@login_required
@require_GET
def receiving_tsd_asset(request, asset_name: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    content_type = _ASSET_CONTENT_TYPES.get(asset_name)
    if content_type is None:
        raise Http404("Ресурс TSD не найден.")
    asset_path = Path(__file__).resolve().parent / "static" / "orders" / asset_name
    if not asset_path.is_file():
        raise Http404("Ресурс TSD не найден.")
    response = FileResponse(asset_path.open("rb"), content_type=content_type)
    response["Cache-Control"] = "private, max-age=300"
    return response


def _json_body(request) -> dict | None:
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _flow_version(entries) -> int:
    for entry in reversed(entries or []):
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        if isinstance(payload.get("flow_state"), dict):
            return ReceivingWorkflowService._parse_flow_client_version(
                payload.get("flow_client_version")
            )
    return 0


def _flow_qty(flow_state: dict | None) -> int:
    state = flow_state if isinstance(flow_state, dict) else {}
    total = 0
    for container in list(state.get("boxes") or []) + list(state.get("pallets") or []):
        if not isinstance(container, dict):
            continue
        total += sum(
            max(ReceivingWorkflowService._parse_int_value(item.get("qty")), 0)
            for item in (container.get("items") or [])
            if isinstance(item, dict)
        )
    return total


def _plan_qty(payload: dict | None) -> int:
    source = payload if isinstance(payload, dict) else {}
    return sum(
        max(ReceivingWorkflowService._parse_int_value(item.get("qty")), 0)
        for item in (source.get("items") or [])
        if isinstance(item, dict)
    )


def _active_box(state: dict) -> dict | None:
    active_code = str(state.get("activeBox") or "").strip()
    for box in state.get("boxes") or []:
        if (
            isinstance(box, dict)
            and str(box.get("code") or "").strip() == active_code
            and not box.get("sealed")
        ):
            return box
    return None


def _active_pallet(state: dict) -> dict | None:
    active_code = str(state.get("activePallet") or "").strip()
    for pallet in state.get("pallets") or []:
        if (
            isinstance(pallet, dict)
            and str(pallet.get("code") or "").strip() == active_code
            and not pallet.get("sealed")
        ):
            return pallet
    return None


def _container_label_already_queued(order_id: str, container_code: str) -> bool:
    code = str(container_code or "").strip()
    if not code:
        return False
    return ProcessingPrintJob.objects.filter(
        order_id=str(order_id or "").strip(),
        barcode=code,
        status__in=(
            ProcessingPrintJob.STATUS_PENDING,
            ProcessingPrintJob.STATUS_PRINTING,
            ProcessingPrintJob.STATUS_PRINTED,
        ),
    ).exists()


def _state_summary(state: dict, *, planned_qty: int = 0) -> dict:
    boxes = [box for box in (state.get("boxes") or []) if isinstance(box, dict)]
    pallets = [pallet for pallet in (state.get("pallets") or []) if isinstance(pallet, dict)]
    active = _active_box(state)
    active_pallet = _active_pallet(state)
    accepted = _flow_qty(state)
    return {
        "accepted_qty": accepted,
        "planned_qty": max(int(planned_qty or 0), 0),
        "remaining_qty": max(int(planned_qty or 0) - accepted, 0),
        "box_count": sum(1 for box in boxes if _box_qty(box) > 0),
        "active_box": str(active.get("code") or "") if active else "",
        "active_box_qty": _box_qty(active),
        "pallet_count": sum(1 for pallet in pallets if _pallet_qty(state, pallet) > 0),
        "sealed_pallet_count": sum(
            1 for pallet in pallets if pallet.get("sealed") and _pallet_qty(state, pallet) > 0
        ),
        "active_pallet": str(active_pallet.get("code") or "") if active_pallet else "",
        "active_pallet_qty": _pallet_qty(state, active_pallet),
        "active_pallet_boxes": len(active_pallet.get("boxes") or []) if active_pallet else 0,
    }


def _box_qty(box: dict | None) -> int:
    if not isinstance(box, dict):
        return 0
    return sum(
        max(ReceivingWorkflowService._parse_int_value(item.get("qty")), 0)
        for item in (box.get("items") or [])
        if isinstance(item, dict)
    )


def _event_journal() -> AuditJournal:
    return AuditJournal.objects.get_or_create(
        code=_EVENT_JOURNAL_CODE,
        defaults={
            "name": "Приемка на TSD",
            "description": "События мобильной приемки и измерение скорости кладовщика.",
        },
    )[0]


def _event_seen(event_id: str) -> bool:
    value = str(event_id or "").strip()[:96]
    if not value:
        return False
    return AuditEntry.objects.filter(
        journal__code=_EVENT_JOURNAL_CODE,
        snapshot__event_id=value,
    ).exists()


def _log_event(
    *,
    request,
    order_id: str,
    event_type: str,
    agency=None,
    event_id: str = "",
    snapshot: dict | None = None,
) -> AuditEntry:
    normalized_type = str(event_type or "").strip()
    if normalized_type not in _EVENT_TYPES:
        raise ValueError("unsupported_receiving_tsd_event")
    payload = dict(snapshot or {})
    payload.update(
        {
            "order_id": str(order_id or "").strip(),
            "event_type": normalized_type,
            "event_id": str(event_id or "").strip()[:96],
            "source": "orders.receiving_tsd",
        }
    )
    return AuditEntry.objects.create(
        journal=_event_journal(),
        action="create",
        agency=agency,
        user=request.user if request.user.is_authenticated else None,
        description=f"TSD приемка: {normalized_type}",
        snapshot=payload,
    )


def _locked_context(order_id: str):
    order_key = str(order_id or "").strip()
    WarehouseCommandService.acquire_receiving_order_lock(order_key)
    OrderAuditEntry.objects.select_for_update().filter(
        order_id=order_key,
        order_type="receiving",
    ).order_by("-created_at", "-id").first()
    context = load_order_context(order_key)
    if context is None:
        raise _TsdStateError("order_not_found", "Заявка не найдена.", http_status=404)
    return context


def _assert_owned(context, request) -> None:
    access = ReceivingWorkflowService.receiving_work_access(
        entries=context.entries,
        user=request.user if request.user.is_authenticated else None,
    )
    if access.status == "assigned_to_other_storekeeper":
        owner = str(access.payload.get("storekeeper_name") or "другого кладовщика")
        raise _TsdStateError(
            access.status,
            f"Заявка уже в работе у {owner}.",
            http_status=409,
        )
    if access.status != "allowed":
        raise _TsdStateError(access.status, "Заявка сейчас недоступна для приемки.", http_status=409)
    owner = ReceivingWorkflowService._receiving_work_owner(context.entries)
    employee = _storekeeper_employee(request)
    if not owner or not employee or owner.get("storekeeper_employee_id") != employee.id:
        raise _TsdStateError("claim_required", "Сначала возьмите заявку в работу.", http_status=409)


def _persist_state(*, context, state: dict, request) -> tuple[dict, int]:
    version = _flow_version(context.entries)
    result = ReceivingWorkflowService.save_receiving_flow_draft(
        order_id=context.order_id,
        entries=context.entries,
        role="storekeeper",
        boxes_raw=json.dumps(state.get("boxes") or [], ensure_ascii=False),
        pallets_raw=json.dumps(state.get("pallets") or [], ensure_ascii=False),
        active_box=str(state.get("activeBox") or ""),
        active_pallet=str(state.get("activePallet") or ""),
        received_at=timezone.localdate().isoformat(),
        draft_version=version + 1,
        draft_base_version=version,
        user=request.user if request.user.is_authenticated else None,
    )
    if result.status != "saved" or result.meta.get("stale_ignored"):
        raise _TsdStateError(
            result.status or "save_failed",
            "Не удалось сохранить скан. Обновите экран и повторите.",
            http_status=409,
        )
    persisted = result.payload.get("flow_state") if isinstance(result.payload, dict) else None
    return (persisted if isinstance(persisted, dict) else state), int(
        result.payload.get("flow_client_version") or version + 1
    )


def _current_state(context) -> dict:
    state = ReceivingWorkflowService.find_receiving_flow_state(context.entries)
    if ReceivingWorkflowService.effective_receiving_mode(context.status_payload) == "cz":
        state = reconcile_flow_state_with_units(
            state,
            list(accepted_units(context.order_id).order_by("accepted_at", "id")),
            active_box=str(state.get("activeBox") or ""),
            active_pallet=str(state.get("activePallet") or ""),
        )
    return state if isinstance(state, dict) else {}


def _saved_receiving_location(entries) -> dict:
    for entry in reversed(entries or []):
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        if str(payload.get("event") or "").strip() != _LOCATION_EVENT:
            continue
        code = str(payload.get("receiving_location_code") or "").strip()
        if code:
            return {
                "code": code,
                "label": str(payload.get("receiving_location_label") or code).strip(),
            }
    return {"code": "", "label": ""}


def _pending_placement_pallets(context, state) -> list[dict]:
    permissions = ReceivingWorkflowService._pallet_placement_permissions_from_entries(context.entries)
    if ReceivingWorkflowService._flow_closed(context.entries):
        return []
    return [
        {"code": str(pallet.get("code") or "").strip(), "qty": _pallet_qty(state, pallet)}
        for pallet in state.get("pallets") or []
        if isinstance(pallet, dict)
        and pallet.get("sealed")
        and _pallet_qty(state, pallet) > 0
        and not permissions.get(str(pallet.get("code") or "").strip().casefold(), False)
    ]


def _pallet_qty(state: dict, pallet: dict | None) -> int:
    if not isinstance(pallet, dict):
        return 0
    boxes_by_code = {
        str(box.get("code") or "").strip(): box
        for box in (state.get("boxes") or [])
        if isinstance(box, dict)
    }
    total = sum(
        max(ReceivingWorkflowService._parse_int_value(item.get("qty")), 0)
        for item in (pallet.get("items") or [])
        if isinstance(item, dict)
    )
    for box_code in pallet.get("boxes") or []:
        total += _box_qty(boxes_by_code.get(str(box_code or "").strip()))
    return total


def _putaway_preview(context, state: dict) -> list[dict]:
    pallets = [
        pallet
        for pallet in (state.get("pallets") or [])
        if isinstance(pallet, dict) and pallet.get("sealed") and _pallet_qty(state, pallet) > 0
    ]
    if not pallets:
        return []
    actual_locations = ReceivingWorkflowService.build_receiving_flow_actual_pallet_locations(
        context.order_id,
        pallets,
    )
    takeout_statuses = ReceivingWorkflowService.build_receiving_flow_pallet_takeout_statuses(
        context.order_id,
        pallets,
    )
    pending_codes = {
        str(row.get("code") or "").strip().casefold()
        for row in _pending_placement_pallets(context, state)
        if str(row.get("code") or "").strip()
    }

    def placement_details(pallet_code: str) -> dict:
        status = takeout_statuses.get(pallet_code) or {}
        current_location = str(actual_locations.get(pallet_code) or "").strip()
        needs_pr_location = pallet_code.casefold() in pending_codes
        if needs_pr_location:
            status_label = "Нужно отсканировать конкретное место PR"
        else:
            status_label = str(status.get("label") or "Место PR сохранено").strip()
        return {
            "current_location_label": current_location or "PR не указано",
            "placement_status": str(status.get("status") or "").strip(),
            "placement_status_label": status_label,
            "needs_pr_location": needs_pr_location,
        }

    if ReceivingWorkflowService.receiving_route_from_entries(context.entries) == "fbs":
        from fbs.services.receiving_movements import receiving_fbs_route_rows

        routed = receiving_fbs_route_rows(
            agency_id=int(getattr(context.agency, "id", 0) or 0),
            order_id=context.order_id,
        )
        routed_by_source = {
            str(row.get("source_pallet_code") or "").strip().casefold(): row
            for row in routed
            if str(row.get("source_pallet_code") or "").strip()
        }
        rows = []
        for pallet in pallets:
            pallet_code = str(pallet.get("code") or "").strip()
            target = routed_by_source.get(pallet_code.casefold()) or {}
            rows.append(
                {
                    "pallet_code": pallet_code,
                    **placement_details(pallet_code),
                    "destination_code": str(target.get("cell_code") or ""),
                    "destination_label": (
                        f"FBS · {target.get('cell_label') or target.get('cell_code')} · "
                        f"паллета {target.get('pallet_code')}"
                        if target
                        else "FBS · закройте палету и отсканируйте место PR для назначения"
                    ),
                }
            )
        return rows
    suggestions = ReceivingWorkflowService.suggest_receiving_destinations(
        pallets,
        exclude_order_type="receiving",
        exclude_order_id=context.order_id,
        agency_id=getattr(context.agency, "id", None),
        row_sections=_OS_ROW_SECTIONS,
        tiers=_OS_TIERS,
        cells_per_tier=_OS_CELLS_PER_TIER,
    )
    rows = []
    for pallet in pallets:
        pallet_code = str(pallet.get("code") or "").strip()
        destination = suggestions.get(pallet_code)
        rows.append(
            {
                "pallet_code": pallet_code,
                **placement_details(pallet_code),
                "destination_code": putaway_location_scan_code(destination) if destination else "",
                "destination_label": putaway_location_label(destination) if destination else "Свободная OS-ячейка не найдена",
            }
        )
    return rows


def _ensure_open_pallet(context, state: dict) -> dict:
    pallet = _active_pallet(state)
    if pallet:
        return pallet
    issued = issue_container_for_order(context, kind="pallet")
    code = str(issued.get("code") or "").strip()
    if not issued.get("ok") or not code:
        raise _TsdStateError("pallet_code_failed", "Не удалось создать палету.")
    pallet = {
        "code": code,
        "boxes": [],
        "items": [],
        "sealed": False,
        "goods_type": str(context.status_payload.get("goods_type") or "").strip(),
        "location": {"zone": "PR"},
    }
    state.setdefault("pallets", []).append(pallet)
    state["activePallet"] = code
    return pallet


def _create_open_box(context, state: dict) -> tuple[dict, dict]:
    if _active_box(state):
        raise _TsdStateError(
            "active_box_exists",
            "Сначала закройте текущий короб.",
            http_status=409,
        )
    pallet = _ensure_open_pallet(context, state)
    issued = issue_container_for_order(
        context,
        kind="box",
        received_at=timezone.localdate().isoformat(),
    )
    box_code = str(issued.get("code") or "").strip()
    if not issued.get("ok") or not box_code:
        raise _TsdStateError("box_code_failed", "Не удалось создать короб.")
    box = {
        "code": box_code,
        "items": [],
        "sealed": False,
        "goods_type": str(context.status_payload.get("goods_type") or "").strip(),
    }
    state.setdefault("boxes", []).append(box)
    state["activeBox"] = box_code
    pallet.setdefault("boxes", []).append(box_code)
    return box, pallet


def _item_payload(item, *, scanned_barcode: str) -> dict:
    sku = item.sku
    return {
        "sku_code": item.sku_code,
        "sku": item.sku_code,
        "barcode": str(scanned_barcode or item.barcode or "").strip(),
        "name": item.name,
        "brand": str(getattr(sku, "brand", "") or "").strip(),
        "color": str(getattr(sku, "color", "") or "").strip(),
        "size": item.size,
        "qty": 1,
    }


_MAX_MANUAL_SCAN_QUANTITY = 100_000


def _parse_scan_quantity(value) -> int:
    if value in (None, ""):
        return 1
    if isinstance(value, bool):
        raise _TsdStateError(
            "invalid_quantity",
            "Количество должно быть целым числом от 1 до 100000.",
        )
    try:
        quantity = int(str(value).strip())
    except (TypeError, ValueError):
        raise _TsdStateError(
            "invalid_quantity",
            "Количество должно быть целым числом от 1 до 100000.",
        )
    if str(quantity) != str(value).strip() or not 1 <= quantity <= _MAX_MANUAL_SCAN_QUANTITY:
        raise _TsdStateError(
            "invalid_quantity",
            "Количество должно быть целым числом от 1 до 100000.",
        )
    return quantity


def _append_unit_to_box(box: dict, item: dict, *, quantity: int = 1) -> None:
    rows = box.setdefault("items", [])
    identity = (
        str(item.get("sku_code") or "").casefold(),
        str(item.get("size") or "").casefold(),
        str(item.get("barcode") or "").casefold(),
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_identity = (
            str(row.get("sku_code") or row.get("sku") or "").casefold(),
            str(row.get("size") or "").casefold(),
            str(row.get("barcode") or "").casefold(),
        )
        if row_identity == identity:
            row["qty"] = (
                max(ReceivingWorkflowService._parse_int_value(row.get("qty")), 0)
                + quantity
            )
            return
    item["qty"] = quantity
    rows.append(item)


def _recent_metrics(order_id: str, *, now=None) -> dict:
    current = now or timezone.localtime()
    events = list(
        AuditEntry.objects.filter(
            journal__code=_EVENT_JOURNAL_CODE,
            snapshot__order_id=str(order_id or "").strip(),
        )
        .only("snapshot", "created_at")
        .order_by("created_at", "id")
    )
    claimed = next(
        (event.created_at for event in events if (event.snapshot or {}).get("event_type") == "claimed"),
        None,
    )
    scans = [event for event in events if (event.snapshot or {}).get("event_type") == "scan_ok"]
    errors = [event for event in events if (event.snapshot or {}).get("event_type") == "scan_error"]
    elapsed_seconds = max((current - claimed).total_seconds(), 0) if claimed else 0
    scanned_qty = sum(
        max(
            ReceivingWorkflowService._parse_int_value(
                (event.snapshot or {}).get("quantity")
            ),
            1,
        )
        for event in scans
    )
    rate = (scanned_qty * 60 / elapsed_seconds) if elapsed_seconds > 0 else 0
    return {
        "started_at": timezone.localtime(claimed).strftime("%H:%M:%S") if claimed else "",
        "elapsed_seconds": int(elapsed_seconds),
        "scanned_qty": scanned_qty,
        "error_count": len(errors),
        "units_per_minute": round(rate, 1),
    }


def _active_desktop_printer(printers: list[str], presence: dict) -> str:
    available = [str(name or "").strip() for name in printers if str(name or "").strip()]
    if not available:
        return ""
    preferred = str(presence.get("preferred_printer") or "").strip()
    detail_by_name = {
        str(item.get("name") or "").strip().casefold(): item
        for item in (presence.get("printer_details") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    if not detail_by_name:
        return preferred if preferred in available else available[0]

    def usable(name: str, *, fallback: bool = False) -> bool:
        detail = detail_by_name.get(name.casefold())
        if not detail or detail.get("is_paused") or detail.get("is_offline"):
            return False
        try:
            jobs = max(int(detail.get("jobs") or 0), 0)
        except (TypeError, ValueError):
            jobs = 0
        if fallback:
            return not detail.get("is_busy") and jobs == 0
        return jobs <= 2

    if preferred in available and usable(preferred):
        return preferred
    default_printer = next(
        (
            name
            for name in available
            if (detail_by_name.get(name.casefold()) or {}).get("is_default")
            and usable(name)
        ),
        "",
    )
    if default_printer:
        return default_printer
    return next((name for name in available if usable(name, fallback=True)), "")


def _agent_rows() -> list[dict]:
    online_after = timezone.now() - timedelta(seconds=45)
    rows = []
    seen_workstations = set()
    agents = list(DeviceAgent.objects.order_by("-last_seen", "-updated_at")[:200])
    desktop_ids_by_agent = {}
    all_desktop_ids = set()
    for agent in agents:
        candidates = []
        agent_id = str(agent.agent_id or "").strip()
        if agent_id:
            candidates.append(agent_id)
        for value in (agent.host, agent.name):
            host = str(value or "").strip().casefold()
            if host:
                candidates.append(f"desktop-{host}")
        candidates = list(dict.fromkeys(candidates))
        desktop_ids_by_agent[agent.pk] = candidates
        all_desktop_ids.update(candidates)
    desktop_presences = get_desktop_presences(all_desktop_ids)

    for agent in agents:
        title = str(agent.name or agent.host or agent.agent_id or "").strip()
        workstation_key = str(agent.host or agent.name or agent.agent_id or "").strip().casefold()
        if not title or workstation_key in seen_workstations:
            continue
        legacy_printers = [
            name
            for name in agent_printer_names_from_meta(
                agent.meta if isinstance(agent.meta, dict) else {}
            )
            if not is_virtual_printer_name(name)
        ]
        desktop_ids = [
            value
            for value in desktop_ids_by_agent.get(agent.pk, [])
            if value in desktop_presences
        ]
        if desktop_ids:
            desktop_ids.sort(
                key=lambda value: str(desktop_presences[value].get("last_seen") or ""),
                reverse=True,
            )
            current_presence = desktop_presences[desktop_ids[0]]
            current_printers = [
                str(name or "").strip()
                for name in (current_presence.get("printers") or [])
                if str(name or "").strip() and not is_virtual_printer_name(name)
            ]
            printers = current_printers or legacy_printers
            preferred_printer = str(current_presence.get("preferred_printer") or "").strip()
            if preferred_printer not in printers:
                preferred_printer = ""
            active_printer = _active_desktop_printer(printers, current_presence)
            target_agent_id = f"desktop:{desktop_ids[0]}"
        elif agent.last_seen and agent.last_seen >= online_after:
            printers = legacy_printers
            preferred_printer = ""
            active_printer = printers[0] if printers else ""
            target_agent_id = str(agent.agent_id or "").strip()
        else:
            continue
        if not target_agent_id or not printers:
            continue
        seen_workstations.add(workstation_key)
        rows.append(
            {
                "agent_id": target_agent_id,
                "title": title,
                "online": True,
                "printers": printers,
                "preferred_printer": preferred_printer,
                "active_printer": active_printer,
                "printer_ready": bool(active_printer),
            }
        )
    return rows


def _receiving_container_codes(state: dict) -> set[str]:
    codes = set()
    for key in ("boxes", "pallets"):
        for row in state.get(key) or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            if code:
                codes.add(code)
    return codes


@login_required
@require_GET
def receiving_tsd_queue(request):
    employee, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    tasks = list(
        Task.objects.filter(
            route__startswith="/orders/receiving/",
            assigned_to__role="storekeeper",
        )
        .exclude(status="done")
        .select_related("assigned_to")
        .order_by("due_date", "created_at")[:200]
    )
    order_ids = []
    task_by_order = {}
    for task in tasks:
        match = _RECEIVING_TASK_ROUTE_RE.match(str(task.route or ""))
        if not match:
            continue
        order_id = match.group(1)
        if order_id not in task_by_order:
            order_ids.append(order_id)
            task_by_order[order_id] = task
    grouped = defaultdict(list)
    if order_ids:
        for entry in (
            OrderAuditEntry.objects.filter(order_type="receiving", order_id__in=order_ids)
            .select_related("agency")
            .order_by("created_at", "id")
        ):
            grouped[str(entry.order_id)].append(entry)
    rows = []
    for order_id in order_ids:
        entries = grouped.get(order_id) or []
        if not entries or ReceivingWorkflowService._flow_closed(entries):
            continue
        status_entry = ReceivingWorkflowService._current_status_entry(entries)
        payload = dict(status_entry.payload or {}) if status_entry else {}
        receiving_route = ReceivingWorkflowService.receiving_route_from_entries(entries)
        payload["receiving_route"] = receiving_route
        state = ReceivingWorkflowService.find_receiving_flow_state(entries)
        latest = entries[-1]
        agency = latest.agency or (status_entry.agency if status_entry else None)
        access = ReceivingWorkflowService.receiving_work_access(
            entries=entries,
            user=request.user,
        )
        if access.status not in {"allowed", "assigned_to_other_storekeeper"}:
            continue
        owner = ReceivingWorkflowService._receiving_work_owner(entries)
        owner_id = int(owner.get("storekeeper_employee_id") or 0) if owner else 0
        owner_name = str(owner.get("storekeeper_name") or "").strip() if owner else ""
        has_owner = bool(owner_id or owner_name)
        is_mine = bool(owner_id and owner_id == employee.id)
        if not has_owner:
            availability = WarehouseCommandService.start_receiving_flow(
                order_id=order_id,
                agency=agency,
                role="storekeeper",
                status_payload=payload,
                flow_closed=False,
            )
            if availability.status != "started":
                continue
        rows.append(
            {
                "order_id": order_id,
                "client": str(
                    getattr(agency, "short_name", "")
                    or getattr(agency, "agn_name", "")
                    or agency
                    or "-"
                ),
                "planned_qty": _plan_qty(payload),
                "accepted_qty": _flow_qty(state),
                "receiving_mode": ReceivingWorkflowService.effective_receiving_mode(payload),
                "goods_type": payload.get("goods_type") or "",
                "goods_type_choices": goods_type_choices(payload),
                "receiving_route": receiving_route,
                "receiving_route_label": ReceivingWorkflowService.receiving_route_label(receiving_route),
                "owner_name": owner_name,
                "is_mine": is_mine,
                "is_available": not has_owner,
                "can_take_over": bool(has_owner and not is_mine),
                "due_at": task_by_order[order_id].due_date,
            }
        )
    rows.sort(key=lambda row: (not row["is_mine"], not row["is_available"], row["due_at"]))
    return render(
        request,
        "orders/receiving_tsd_queue.html",
        {
            "employee": employee,
            "rows": rows,
            "page_title": "Приемка на TSD",
        },
    )


@login_required
@require_POST
def receiving_tsd_claim(request, order_id: str):
    employee, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    with transaction.atomic():
        WarehouseCommandService.acquire_receiving_order_lock(str(order_id or ""))
        entries = list(
            OrderAuditEntry.objects.select_for_update()
            .filter(order_id=order_id, order_type="receiving")
            .order_by("created_at", "id")
        )
        if not entries:
            raise Http404("Заявка не найдена")
        result = claim_tsd_receiving(
            order_id=str(order_id or ""),
            entries=entries,
            role="storekeeper",
            user=request.user,
            goods_type=request.POST.get("goods_type") or "",
        )
        if result.status == "assigned_to_other_storekeeper":
            owner_name = result.payload.get("storekeeper_name") or "другого кладовщика"
            if str(request.POST.get("take_over") or "").strip() != "1":
                messages.error(request, f"Заявка уже в работе у {owner_name}.")
                return redirect("orders-receiving-tsd-queue")
            transfer = ReceivingWorkflowService.take_over_receiving_work(
                order_id=str(order_id or ""),
                role="storekeeper",
                user=request.user,
            )
            if transfer.status not in {"taken_over", "already_owned"}:
                messages.error(request, "Не удалось передать заявку. Обновите очередь и повторите.")
                return redirect("orders-receiving-tsd-queue")
            if transfer.status == "taken_over":
                _log_event(
                    request=request,
                    order_id=str(order_id or ""),
                    event_type="claimed",
                    agency=entries[-1].agency,
                    snapshot={
                        "employee_id": employee.id,
                        "employee_name": employee.full_name,
                        "taken_over": True,
                        "previous_employee_name": owner_name,
                    },
                )
                messages.success(request, f"Заявка передана от {owner_name}.")
            return redirect("orders-receiving-tsd-detail", order_id=order_id)
        if result.status not in {"started", "already_in_progress"}:
            messages.error(request, result.reason if result.status == "invalid_goods_type" else "Заявку сейчас нельзя взять в работу.")
            return redirect("orders-receiving-tsd-queue")
        if result.status == "started":
            claim_payload = dict(result.payload or {})
            claim_payload.update(
                {
                    "receiving_tsd_claimed": True,
                    "receiving_tsd_claimed_at": timezone.localtime().isoformat(),
                    "storekeeper_employee_id": employee.id,
                    "storekeeper_name": employee.full_name,
                }
            )
            log_order_action(
                "status",
                order_id=str(order_id or ""),
                order_type="receiving",
                user=request.user,
                agency=entries[-1].agency,
                description="Заявка взята в работу кладовщиком через TSD",
                payload=claim_payload,
            )
            _log_event(
                request=request,
                order_id=str(order_id or ""),
                event_type="claimed",
                agency=entries[-1].agency,
                snapshot={"employee_id": employee.id, "employee_name": employee.full_name},
            )
    return redirect("orders-receiving-tsd-detail", order_id=order_id)


@login_required
@require_GET
def receiving_tsd_detail(request, order_id: str):
    employee, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    context = load_order_context(order_id)
    if context is None:
        raise Http404("Заявка не найдена")
    access = ReceivingWorkflowService.receiving_work_access(entries=context.entries, user=request.user)
    owner = ReceivingWorkflowService._receiving_work_owner(context.entries)
    if access.status == "assigned_to_other_storekeeper":
        messages.error(
            request,
            f"Заявка уже в работе у {access.payload.get('storekeeper_name') or 'другого кладовщика'}.",
        )
        return redirect("orders-receiving-tsd-queue")
    if not owner:
        receiving_route = ReceivingWorkflowService.receiving_route_from_entries(context.entries)
        claim_payload = dict(context.status_payload)
        claim_payload["receiving_route"] = receiving_route
        return render(
            request,
            "orders/receiving_tsd_claim.html",
            {"employee": employee, "order_id": order_id, "client": context.agency,
             "goods_type": context.status_payload.get("goods_type") or "",
             "goods_type_choices": goods_type_choices(claim_payload),
             "receiving_route": receiving_route,
             "receiving_route_label": ReceivingWorkflowService.receiving_route_label(receiving_route)},
        )
    state = _current_state(context)
    if "cz_recount_box" in request.GET:
        # Read-only snapshot, scoped to the authorized receiving order and box.
        box_code = str(request.GET.get("cz_recount_box") or "").strip()
        if not box_code or box_code not in {
            str(box.get("code") or "").strip()
            for box in state.get("boxes") or [] if isinstance(box, dict)
        }:
            return JsonResponse({"ok": False, "error": "Короб не найден в этой приемке."}, status=404)
        response = JsonResponse({
            "ok": True,
            "box_code": box_code,
            "marking_codes": list(accepted_units(order_id).filter(box_code=box_code).values_list("marking_code", flat=True)),
        })
        response["Cache-Control"] = "no-store"
        return response
    planned = _plan_qty(context.status_payload)
    summary = _state_summary(state, planned_qty=planned)
    mode = ReceivingWorkflowService.effective_receiving_mode(context.status_payload)
    chz_service = ReceivingWorkflowService.normalize_receiving_chz_service(
        context.status_payload.get("receiving_chz_service")
    )
    receiving_configuration_lock = ReceivingWorkflowService.receiving_configuration_lock(
        context.entries
    )
    mode = str(
        receiving_configuration_lock.get("receiving_mode") or mode
    ).strip().lower()
    goods_type = str(
        receiving_configuration_lock.get("goods_type")
        or context.status_payload.get("goods_type")
        or ""
    ).strip().lower()
    receiving_route = ReceivingWorkflowService.receiving_route_from_entries(context.entries)
    detail_payload = dict(context.status_payload)
    detail_payload["receiving_route"] = receiving_route
    has_accepted_items = ReceivingWorkflowService.receiving_flow_has_items(state)
    marking_mode_required = chz_service == "required"
    marking_mode_can_change = bool(
        not marking_mode_required
        and goods_type != "no"
        and not has_accepted_items
        and not receiving_configuration_lock
    )
    if marking_mode_required:
        marking_mode_hint = "Клиент указал обязательное сканирование Честного знака."
    elif receiving_configuration_lock:
        marking_mode_hint = "Тип и режим приемки зафиксированы при начале работы."
    elif goods_type == "no":
        marking_mode_hint = "Для типа «Не обработанный» сканирование Честного знака недоступно."
    elif has_accepted_items:
        marking_mode_hint = "Режим зафиксирован после первого принятого товара."
    else:
        marking_mode_hint = "При необходимости включите до первого скана товара."
    receiving_location = _saved_receiving_location(context.entries)
    print_jobs = list(
        ProcessingPrintJob.objects.filter(order_id=str(order_id or ""))
        .exclude(barcode="")
        .order_by("-created_at")[:10]
    )
    return render(
        request,
        "orders/receiving_tsd_detail.html",
        {
            "employee": employee,
            "order_id": order_id,
            "client": context.agency,
            "receiving_mode": mode,
            "goods_type": goods_type,
            "goods_type_label": ReceivingWorkflowService.RECEIVING_GOODS_TYPE_LABELS.get(goods_type, "Не выбран"),
            "goods_type_choices": goods_type_choices(detail_payload),
            "receiving_route": receiving_route,
            "receiving_route_label": ReceivingWorkflowService.receiving_route_label(receiving_route),
            "is_cz": mode == "cz",
            "marking_mode_required": marking_mode_required,
            "marking_mode_can_change": marking_mode_can_change,
            "marking_mode_hint": marking_mode_hint,
            "receiving_configuration_locked": bool(receiving_configuration_lock),
            "flow_state": state,
            "summary": summary,
            "receiving_location": receiving_location,
            "pending_placement_pallets": _pending_placement_pallets(context, state),
            "putaway_preview": _putaway_preview(context, state),
            "metrics": _recent_metrics(order_id),
            "agents": _agent_rows(),
            "print_jobs": print_jobs,
            "flow_version": _flow_version(context.entries),
        },
    )


@login_required
@require_POST
def receiving_tsd_set_goods_type(request, order_id: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    result = configure_tsd_goods_type(
        order_id=order_id, goods_type=request.POST.get("goods_type") or "",
        role="storekeeper", user=request.user,
    )
    if result.applied:
        messages.success(request, "Тип новых коробов сохранен. Уже принятый товар не изменен.")
    else:
        messages.error(request, result.description or "Не удалось изменить тип товара.")
    return redirect("orders-receiving-tsd-detail", order_id=order_id)


@login_required
@require_GET
def receiving_tsd_print_agents(request, order_id: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    context = load_order_context(order_id)
    if context is None:
        return JsonResponse({"ok": False, "error": "Заявка не найдена."}, status=404)
    try:
        _assert_owned(context, request)
    except _TsdStateError as exc:
        return JsonResponse({"ok": False, "status": exc.status, "error": exc.message}, status=exc.http_status)
    return JsonResponse(
        {
            "ok": True,
            "agents": _agent_rows(),
            "refreshed_at": timezone.localtime().isoformat(),
        }
    )


@login_required
@require_POST
def receiving_tsd_print_label(request, order_id: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Некорректные данные печати."}, status=400)
    context = load_order_context(order_id)
    if context is None:
        return JsonResponse({"ok": False, "error": "Заявка не найдена."}, status=404)
    try:
        _assert_owned(context, request)
    except _TsdStateError as exc:
        return JsonResponse({"ok": False, "status": exc.status, "error": exc.message}, status=exc.http_status)

    agent_id = str(payload.get("agent_id") or "").strip()
    live_agent = next(
        (row for row in _agent_rows() if row.get("agent_id") == agent_id),
        None,
    )
    if live_agent is None:
        return JsonResponse(
            {
                "ok": False,
                "error": "Компьютер больше не в сети. Выберите другой включенный компьютер.",
            },
            status=409,
        )
    printer_name = str(live_agent.get("active_printer") or "").strip()
    if not printer_name:
        return JsonResponse(
            {
                "ok": False,
                "error": "На выбранном компьютере нет готового к печати принтера. Проверьте принтер в верхней строке Fullbox Desktop.",
            },
            status=409,
        )

    container_code = str(payload.get("barcode") or "").strip()
    if container_code not in _receiving_container_codes(_current_state(context)):
        return JsonResponse(
            {"ok": False, "error": "Этот короб или палета не относится к текущей приемке."},
            status=409,
        )
    print_payload = dict(payload)
    print_payload.update(
        {
            "order_id": str(order_id or "").strip(),
            "agent_id": agent_id,
            "printer_name": printer_name,
            "barcode": container_code,
        }
    )
    result = ProcessingWorkflowService.enqueue_processing_print_job(
        request=request,
        data=print_payload,
    )
    return JsonResponse(result.payload, status=result.http_status)


@login_required
@require_POST
def receiving_tsd_set_marking_mode(request, order_id: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    payload = _json_body(request)
    if payload is None or not isinstance(payload.get("enabled"), bool):
        return JsonResponse({"ok": False, "error": "Некорректный режим Честного знака."}, status=400)
    requested_mode = "cz" if payload["enabled"] else "standard"
    result = ReceivingWorkflowService.set_receiving_marking_mode(
        order_id=str(order_id or ""),
        role="storekeeper",
        receiving_mode=requested_mode,
        user=request.user,
    )
    if not result.applied:
        return JsonResponse(
            {"ok": False, "error": result.description or "Не удалось изменить режим Честного знака."},
            status=409,
        )
    mode = ReceivingWorkflowService.effective_receiving_mode(result.payload)
    return JsonResponse(
        {
            "ok": True,
            "receiving_mode": mode,
            "is_cz": mode == "cz",
            "message": (
                "Сканирование Честного знака включено."
                if mode == "cz"
                else "Обычная приемка без обязательного сканирования Честного знака."
            ),
        }
    )


@login_required
@require_POST
def receiving_tsd_open_box(request, order_id: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Некорректный запрос."}, status=400)
    event_id = str(payload.get("event_id") or "").strip()
    try:
        with transaction.atomic():
            context = _locked_context(order_id)
            _assert_owned(context, request)
            if event_id and _event_seen(event_id):
                state = _current_state(context)
                return JsonResponse(
                    {"ok": True, "duplicate_ignored": True, "state": state, "summary": _state_summary(state, planned_qty=_plan_qty(context.status_payload))}
                )
            state = _current_state(context)
            box, pallet = _create_open_box(context, state)
            box_code = str(box.get("code") or "")
            state, version = _persist_state(context=context, state=state, request=request)
            _log_event(
                request=request,
                order_id=context.order_id,
                event_type="box_opened",
                agency=context.agency,
                event_id=event_id,
                snapshot={"box_code": box_code, "pallet_code": pallet.get("code") or ""},
            )
            return JsonResponse(
                {
                    "ok": True,
                    "state": state,
                    "flow_version": version,
                    "summary": _state_summary(state, planned_qty=_plan_qty(context.status_payload)),
                }
            )
    except _TsdStateError as exc:
        return JsonResponse({"ok": False, "error": exc.message, "status": exc.status}, status=exc.http_status)


@login_required
@require_POST
def receiving_tsd_scan(request, order_id: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Некорректный запрос."}, status=400)
    barcode = str(payload.get("barcode") or "").strip()
    marking_code = str(payload.get("marking_code") or "").strip()
    event_id = str(payload.get("event_id") or "").strip()
    if not barcode:
        return JsonResponse({"ok": False, "error": "Штрихкод не указан."}, status=400)
    try:
        quantity = _parse_scan_quantity(payload.get("quantity"))
        with transaction.atomic():
            context = _locked_context(order_id)
            _assert_owned(context, request)
            if event_id and _event_seen(event_id):
                state = _current_state(context)
                return JsonResponse(
                    {"ok": True, "duplicate_ignored": True, "state": state, "summary": _state_summary(state, planned_qty=_plan_qty(context.status_payload))}
                )
            state = _current_state(context)
            box = _active_box(state)
            pallet = _active_pallet(state)
            item = _find_item_by_barcode(context, barcode)
            if item is None:
                _log_event(
                    request=request,
                    order_id=context.order_id,
                    event_type="scan_error",
                    agency=context.agency,
                    event_id=event_id,
                    snapshot={"barcode": barcode, "reason": "barcode_not_found"},
                )
                raise _TsdStateError("barcode_not_found", "ШК не найден в номенклатуре клиента.")
            is_marked = ReceivingWorkflowService.effective_receiving_mode(
                context.status_payload
            ) == "cz"
            if is_marked and quantity != 1:
                raise _TsdStateError(
                    "marked_quantity_must_be_one",
                    "Товар с Честным знаком принимается только по 1 штуке с отдельным Data Matrix.",
                )
            if is_marked and not marking_code:
                return JsonResponse(
                    {
                        "ok": True,
                        "requires_marking": True,
                        "item": _item_payload(item, scanned_barcode=barcode),
                        "summary": _state_summary(state, planned_qty=_plan_qty(context.status_payload)),
                    }
                )
            box_opened_automatically = False
            if not box:
                box, pallet = _create_open_box(context, state)
                box_opened_automatically = True
            elif not pallet:
                raise _TsdStateError(
                    "pallet_required",
                    "У открытого короба не найдена палета. Обновите экран.",
                    http_status=409,
                )
            if is_marked:
                result = scan_unit(
                    context=context,
                    barcode=barcode,
                    marking_code=marking_code,
                    box_code=str(box.get("code") or ""),
                    pallet_code=str(pallet.get("code") or ""),
                    user=request.user,
                )
                if result.status != "ok":
                    _log_event(
                        request=request,
                        order_id=context.order_id,
                        event_type="scan_error",
                        agency=context.agency,
                        event_id=event_id,
                        snapshot={"barcode": barcode, "reason": result.status},
                    )
                    raise _TsdStateError(
                        result.status,
                        result.error or "Код ЧЗ не принят.",
                        http_status=409 if result.status in {"duplicate", "conflict"} else 400,
                    )
                state = reconcile_flow_state_with_units(
                    state,
                    list(accepted_units(context.order_id).order_by("accepted_at", "id")),
                    active_box=str(box.get("code") or ""),
                    active_pallet=str(pallet.get("code") or ""),
                )
                accepted_item = serialize_unit(result.unit)
            else:
                accepted_item = _item_payload(item, scanned_barcode=barcode)
                _append_unit_to_box(box, accepted_item, quantity=quantity)
            state, version = _persist_state(context=context, state=state, request=request)
            if box_opened_automatically:
                _log_event(
                    request=request,
                    order_id=context.order_id,
                    event_type="box_opened",
                    agency=context.agency,
                    event_id=event_id,
                    snapshot={
                        "box_code": str(box.get("code") or ""),
                        "pallet_code": str(pallet.get("code") or ""),
                        "automatic": True,
                    },
                )
            _log_event(
                request=request,
                order_id=context.order_id,
                event_type="scan_ok",
                agency=context.agency,
                event_id=event_id,
                snapshot={
                    "barcode": barcode,
                    "sku_code": item.sku_code,
                    "box_code": str(box.get("code") or ""),
                    "pallet_code": str(pallet.get("code") or ""),
                    "marked": is_marked,
                    "quantity": quantity,
                    "unit_id": getattr(result.unit, "id", 0) if is_marked else 0,
                },
            )
            return JsonResponse(
                {
                    "ok": True,
                    "requires_marking": False,
                    "item": accepted_item,
                    "accepted_quantity": quantity,
                    "state": state,
                    "flow_version": version,
                    "summary": _state_summary(state, planned_qty=_plan_qty(context.status_payload)),
                    "metrics": _recent_metrics(context.order_id),
                    "box_opened_automatically": box_opened_automatically,
                    "box_code": str(box.get("code") or ""),
                }
            )
    except _TsdStateError as exc:
        try:
            error_context = load_order_context(order_id)
            if error_context is not None and not _event_seen(event_id):
                _log_event(
                    request=request,
                    order_id=error_context.order_id,
                    event_type="scan_error",
                    agency=error_context.agency,
                    event_id=event_id,
                    snapshot={"barcode": barcode, "reason": exc.status},
                )
        except Exception:
            pass
        return JsonResponse({"ok": False, "error": exc.message, "status": exc.status}, status=exc.http_status)


@login_required
@require_POST
def receiving_tsd_close_box(request, order_id: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Некорректный запрос."}, status=400)
    event_id = str(payload.get("event_id") or "").strip()
    try:
        with transaction.atomic():
            context = _locked_context(order_id)
            _assert_owned(context, request)
            state = _current_state(context)
            if event_id and _event_seen(event_id):
                return JsonResponse(
                    {
                        "ok": True,
                        "duplicate_ignored": True,
                        "state": state,
                        "summary": _state_summary(
                            state,
                            planned_qty=_plan_qty(context.status_payload),
                        ),
                    }
                )
            box = _active_box(state)
            if not box:
                raise _TsdStateError("box_not_found", "Открытый короб не найден.", http_status=409)
            if _box_qty(box) <= 0:
                raise _TsdStateError("empty_box", "Нельзя закрыть пустой короб.", http_status=409)
            box["sealed"] = True
            box_code = str(box.get("code") or "")
            state["activeBox"] = ""
            state, version = _persist_state(context=context, state=state, request=request)
            _log_event(
                request=request,
                order_id=context.order_id,
                event_type="box_closed",
                agency=context.agency,
                event_id=event_id,
                snapshot={"box_code": box_code, "qty": _box_qty(box)},
            )
            return JsonResponse(
                {
                    "ok": True,
                    "box_code": box_code,
                    "state": state,
                    "flow_version": version,
                    "summary": _state_summary(state, planned_qty=_plan_qty(context.status_payload)),
                    "metrics": _recent_metrics(context.order_id),
                }
            )
    except _TsdStateError as exc:
        return JsonResponse({"ok": False, "error": exc.message, "status": exc.status}, status=exc.http_status)


@login_required
@require_POST
def receiving_tsd_close_pallet(request, order_id: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Некорректный запрос."}, status=400)
    event_id = str(payload.get("event_id") or "").strip()
    try:
        with transaction.atomic():
            context = _locked_context(order_id)
            _assert_owned(context, request)
            state = _current_state(context)
            if event_id and _event_seen(event_id):
                return JsonResponse(
                    {
                        "ok": True,
                        "duplicate_ignored": True,
                        "state": state,
                        "summary": _state_summary(
                            state,
                            planned_qty=_plan_qty(context.status_payload),
                        ),
                        "putaway_preview": _putaway_preview(context, state),
                    }
                )
            if _active_box(state):
                raise _TsdStateError(
                    "active_box_exists",
                    "Сначала закройте текущий короб.",
                    http_status=409,
                )
            pallet = _active_pallet(state)
            if not pallet:
                raise _TsdStateError(
                    "pallet_not_found",
                    "Открытая палета не найдена.",
                    http_status=409,
                )
            if _pallet_qty(state, pallet) <= 0:
                raise _TsdStateError(
                    "empty_pallet",
                    "Нельзя закрыть пустую палету.",
                    http_status=409,
                )
            pallet["sealed"] = True
            pallet_code = str(pallet.get("code") or "")
            print_required = not _container_label_already_queued(
                context.order_id,
                pallet_code,
            )
            state["activePallet"] = ""
            state, version = _persist_state(context=context, state=state, request=request)
            _log_event(
                request=request,
                order_id=context.order_id,
                event_type="pallet_closed",
                agency=context.agency,
                event_id=event_id,
                snapshot={
                    "pallet_code": pallet_code,
                    "qty": _pallet_qty(state, pallet),
                    "box_count": len(pallet.get("boxes") or []),
                },
            )
            refreshed = load_order_context(context.order_id) or context
            return JsonResponse(
                {
                    "ok": True,
                    "pallet_code": pallet_code,
                    "print_required": print_required,
                    "state": state,
                    "flow_version": version,
                    "summary": _state_summary(
                        state,
                        planned_qty=_plan_qty(context.status_payload),
                    ),
                    "putaway_preview": _putaway_preview(refreshed, state),
                    "pending_placement_pallets": _pending_placement_pallets(refreshed, state),
                }
            )
    except _TsdStateError as exc:
        return JsonResponse({"ok": False, "error": exc.message, "status": exc.status}, status=exc.http_status)


@login_required
@require_POST
def receiving_tsd_set_location(request, order_id: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Некорректный запрос."}, status=400)
    location_code = str(payload.get("location_code") or "").strip()
    pallet_code = str(payload.get("pallet_code") or "").strip()
    event_id = str(payload.get("event_id") or "").strip()
    if not location_code:
        return JsonResponse({"ok": False, "error": "Отсканируйте место приемки PR."}, status=400)
    try:
        with transaction.atomic():
            context = _locked_context(order_id)
            _assert_owned(context, request)
            location = WarehouseWritePathService.concrete_movement_destination(
                warehouse_code="MSK",
                zone_code="PR",
                location_code=location_code,
            )
            try:
                require_receiving_location(location)
            except ValidationError as exc:
                raise ValueError("; ".join(exc.messages)) from exc
            canonical_code = str(location.location_code or "").strip()
            label = str(location.display_name or canonical_code).strip()
            state = _current_state(context)
            selected_pallet = next(
                (
                    pallet
                    for pallet in state.get("pallets") or []
                    if isinstance(pallet, dict)
                    and str(pallet.get("code") or "").strip().casefold() == pallet_code.casefold()
                ),
                None,
            )
            if pallet_code and not selected_pallet:
                raise ValueError("Палета не найдена в этой заявке. Обновите страницу и выберите нужную палету.")
            if selected_pallet and (not selected_pallet.get("sealed") or _pallet_qty(state, selected_pallet) <= 0):
                raise ValueError("Для размещения выберите закрытую непустую палету.")
            if not pallet_code and _pending_placement_pallets(context, state):
                raise ValueError("Не указана палета для места PR. Обновите страницу и выберите закрытую палету: адрес не сохранён.")
            pending_pallet_code = str((selected_pallet or {}).get("code") or "").strip()
            if event_id:
                previous = AuditEntry.objects.filter(
                    journal__code=_EVENT_JOURNAL_CODE,
                    snapshot__order_id=context.order_id,
                    snapshot__event_type="location_selected",
                    snapshot__event_id=event_id,
                ).first()
                if previous:
                    saved = previous.snapshot or {}
                    if saved.get("pallet_code", "") != pending_pallet_code or saved.get("location_code") != canonical_code:
                        raise ValueError("Этот скан уже обработан для другой палеты или места. Повторите сканирование.")
                    return JsonResponse({
                        "ok": True, "duplicate_ignored": True,
                        "receiving_location": {"code": canonical_code, "label": label},
                        "placement_pallet_code": pending_pallet_code,
                        "pending_placement_pallets": _pending_placement_pallets(context, state),
                    })
            # Do not record a new address for an already released/moved pallet.
            if pending_pallet_code:
                permissions = ReceivingWorkflowService._pallet_placement_permissions_from_entries(context.entries)
                if permissions.get(pending_pallet_code.casefold()) or ReceivingWorkflowService._flow_closed(context.entries):
                    raise ValueError("Размещение этой палеты уже разрешено. Адрес не изменён; обновите страницу.")
                takeout = ReceivingWorkflowService.build_receiving_flow_pallet_takeout_statuses(
                    context.order_id, [selected_pallet],
                ).get(pending_pallet_code, {})
                if takeout.get("status") in {"placed", "moving", "depleted"}:
                    raise ValueError("Палета уже перемещена или находится в работе. Адрес приёмки не изменён.")
            log_order_action(
                "update",
                order_id=context.order_id,
                order_type="receiving",
                user=request.user,
                agency=context.agency,
                description=f"На TSD указано место приемки {canonical_code}",
                payload={
                    "event": _LOCATION_EVENT,
                    "receiving_location_code": canonical_code,
                    "receiving_location_label": label,
                    "receiving_location_selected_at": timezone.localtime().isoformat(),
                    "pallet_code": pending_pallet_code,
                },
            )
            _log_event(
                request=request,
                order_id=context.order_id,
                event_type="location_selected",
                agency=context.agency,
                event_id=event_id,
                snapshot={
                    "location_code": canonical_code,
                    "location_label": label,
                    "pallet_code": pending_pallet_code,
                },
            )
            placement_result = None
            if pending_pallet_code:
                placement_result = ReceivingWorkflowService.allow_receiving_flow_pallet_placement(
                    order_id=context.order_id,
                    pallet_code=pending_pallet_code,
                    receiving_location_code=canonical_code,
                    fbs_cell_id=payload.get("fbs_cell_id"),
                    user=request.user,
                )
                if str(placement_result.get("status") or "") != "allowed":
                    raise ValueError(
                        placement_result.get("message")
                        or "Не удалось разрешить размещение закрытой палеты."
                    )
            return JsonResponse(
                {
                    "ok": True,
                    "receiving_location": {"code": canonical_code, "label": label},
                    "placement_pallet_code": pending_pallet_code,
                    "placement_status": (
                        placement_result.get("takeout_status") if placement_result else {}
                    ),
                    "fbs_route": placement_result.get("fbs_route") if placement_result else {},
                    "putaway_preview": _putaway_preview(
                        load_order_context(context.order_id), state,
                    ),
                    "pending_placement_pallets": _pending_placement_pallets(
                        load_order_context(context.order_id), state,
                    ),
                }
            )
    except (ValueError, ValidationError) as exc:
        return JsonResponse(
            {"ok": False, "error": str(exc) or "Место PR не найдено."},
            status=409,
        )
    except _TsdStateError as exc:
        return JsonResponse({"ok": False, "error": exc.message, "status": exc.status}, status=exc.http_status)


@login_required
@require_POST
def receiving_tsd_complete(request, order_id: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Некорректный запрос."}, status=400)
    event_id = str(payload.get("event_id") or "").strip()
    try:
        with transaction.atomic():
            context = _locked_context(order_id)
            if ReceivingWorkflowService._flow_closed(context.entries):
                return JsonResponse(
                    {
                        "ok": True,
                        "duplicate_ignored": True,
                        "redirect_url": "/orders/receiving/tsd/",
                    }
                )
            _assert_owned(context, request)
            state = _current_state(context)
            if _active_box(state):
                raise _TsdStateError("active_box_exists", "Сначала закройте текущий короб.", http_status=409)
            if _active_pallet(state):
                raise _TsdStateError("active_pallet_exists", "Сначала закройте текущую палету.", http_status=409)
            pallets = [
                pallet
                for pallet in (state.get("pallets") or [])
                if isinstance(pallet, dict) and _pallet_qty(state, pallet) > 0
            ]
            if not pallets or any(not pallet.get("sealed") for pallet in pallets):
                raise _TsdStateError("sealed_pallet_required", "Закройте все палеты перед завершением.", http_status=409)
            pending_placement = _pending_placement_pallets(context, state)
            if pending_placement:
                pending_codes = [
                    str(row.get("code") or "").strip()
                    for row in pending_placement
                    if str(row.get("code") or "").strip()
                ]
                raise _TsdStateError(
                    "pallet_location_required",
                    "Не указано конкретное место PR для палет: "
                    + ", ".join(pending_codes)
                    + ". Отсканируйте место каждой закрытой палеты.",
                    http_status=409,
                )
            receiving_route = ReceivingWorkflowService.receiving_route_from_entries(
                context.entries
            )
            if receiving_route == "fbs":
                fbs_preview = _putaway_preview(context, state)
                missing_fbs = [
                    row.get("pallet_code")
                    for row in fbs_preview
                    if not str(row.get("destination_code") or "").strip()
                ]
                if missing_fbs:
                    raise _TsdStateError(
                        "fbs_destination_required",
                        "Не назначено точное FBS-место для палет: "
                        + ", ".join(str(code) for code in missing_fbs)
                        + ". Освободите FBS-место и повторно сохраните место PR.",
                        http_status=409,
                    )
            receiving_location = _saved_receiving_location(context.entries)
            if not receiving_location.get("code"):
                raise _TsdStateError("location_required", "Отсканируйте конкретное место приемки PR.", http_status=409)

            boxes_raw = json.dumps(state.get("boxes") or [], ensure_ascii=False)
            pallets_raw = json.dumps(state.get("pallets") or [], ensure_ascii=False)
            preparation = ReceivingWorkflowService.prepare_receiving_flow_completion(
                order_id=context.order_id,
                entries=context.entries,
                boxes_raw=boxes_raw,
                pallets_raw=pallets_raw,
            )
            if preparation.status != "ok":
                reason_messages = {
                    "missing_containers": "Нет закрытых коробов и палет.",
                    "unassigned_boxes": "Есть короб, не добавленный в палету.",
                    "orphan_units": "Есть код ЧЗ без короба или палеты.",
                    "marking_mismatch": "Количество кодов ЧЗ не совпадает с количеством товара.",
                    "missing_items": "В приемке нет товара.",
                }
                raise _TsdStateError(
                    preparation.reason or "invalid_receiving",
                    reason_messages.get(preparation.reason, "Проверьте состав приемки."),
                    http_status=409,
                )

            ReceivingWorkflowService.complete_receiving_flow(
                order_id=context.order_id,
                agency=context.agency,
                status_payload=preparation.status_payload,
                has_mismatch=preparation.has_mismatch,
                receiving_mode=preparation.receiving_mode,
                act_items=preparation.act_items,
                placement_items=preparation.placement_items,
                boxes=preparation.boxes,
                pallets=preparation.pallets,
                flow_state=preparation.flow_state,
                act_units=preparation.act_units,
                eta_at=preparation.normalized_eta,
                received_at=timezone.localtime().isoformat(),
                vehicle_number=preparation.vehicle_number,
                has_closed_placement_act=preparation.has_closed_placement_act,
                flow_client_version=_flow_version(context.entries),
                receiving_location_code=receiving_location["code"],
                user=request.user,
                submitted_at=timezone.localtime(),
            )

            try:
                from receiving_distribution.services.lifecycle import sync_accepted_from_flow_boxes
                from receiving_distribution.services.reserve_linked import sync_linked_shipping_reserves

                sync_accepted_from_flow_boxes(
                    receiving_order_id=context.order_id,
                    boxes=boxes_raw,
                    pallets=pallets_raw,
                    user=request.user,
                    source="orders.receiving_tsd.complete",
                )
                sync_linked_shipping_reserves(
                    receiving_order_id=context.order_id,
                    user=request.user,
                    source="orders.receiving_tsd.complete",
                )
            except Exception:
                pass

            entries = list(
                OrderAuditEntry.objects.filter(
                    order_id=context.order_id,
                    order_type="receiving",
                )
                .select_related("agency")
                .order_by("created_at", "id")
            )
            pallet_codes = [str(pallet.get("code") or "").strip() for pallet in pallets]
            destinations = ReceivingWorkflowService.collect_receiving_warehouse_move_destinations(
                order_id=context.order_id,
                entries=entries,
                pallet_codes=pallet_codes,
                agency_id=getattr(context.agency, "id", None),
                row_sections=_OS_ROW_SECTIONS,
                tiers=_OS_TIERS,
                cells_per_tier=_OS_CELLS_PER_TIER,
            )
            move_result = None
            if destinations:
                move_result = ReceivingWorkflowService.create_receiving_warehouse_moves(
                    order_id=context.order_id,
                    entries=entries,
                    role="storekeeper",
                    user=request.user,
                    destinations_by_pallet=destinations,
                )
            created_count = int(getattr(move_result, "created_count", 0) or 0)
            missing_count = max(len(pallet_codes) - created_count, 0)
            _log_event(
                request=request,
                order_id=context.order_id,
                event_type="completed",
                agency=context.agency,
                event_id=event_id,
                snapshot={
                    "receiving_location_code": receiving_location["code"],
                    "pallet_count": len(pallet_codes),
                    "putaway_created_count": created_count,
                    "putaway_missing_count": missing_count,
                },
            )
            if receiving_route == "fbs":
                message = f"Приемка завершена. Точные FBS-маршруты назначены: {len(pallet_codes)}."
                missing_count = 0
            else:
                message = f"Приемка завершена. Заданий ричтраку: {created_count}."
                if missing_count:
                    message += f" Без свободной OS-ячейки: {missing_count}."
            messages.success(request, message)
            return JsonResponse(
                {
                    "ok": True,
                    "message": message,
                    "created_count": created_count,
                    "missing_count": missing_count,
                    "redirect_url": "/orders/receiving/tsd/",
                }
            )
    except _TsdStateError as exc:
        return JsonResponse({"ok": False, "error": exc.message, "status": exc.status}, status=exc.http_status)
    except PermissionError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=403)
    except (ValueError, ValidationError) as exc:
        message = "; ".join(str(item) for item in getattr(exc, "messages", []) if str(item)) or str(exc)
        return JsonResponse({"ok": False, "error": message or "Не удалось завершить приемку."}, status=409)


@login_required
@require_POST
def receiving_tsd_event(request, order_id: str):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Некорректный запрос."}, status=400)
    event_type = str(payload.get("event_type") or "").strip()
    if event_type not in {"print_queued", "print_failed"}:
        return JsonResponse({"ok": False, "error": "Недопустимое событие."}, status=400)
    context = load_order_context(order_id)
    if context is None:
        return JsonResponse({"ok": False, "error": "Заявка не найдена."}, status=404)
    _log_event(
        request=request,
        order_id=context.order_id,
        event_type=event_type,
        agency=context.agency,
        event_id=str(payload.get("event_id") or ""),
        snapshot={
            "container_type": str(payload.get("container_type") or "")[:32],
            "container_code": str(
                payload.get("container_code") or payload.get("box_code") or ""
            )[:128],
            "job_id": ReceivingWorkflowService._parse_int_value(payload.get("job_id")),
            "agent_id": str(payload.get("agent_id") or "")[:128],
            "printer_name": str(payload.get("printer_name") or "")[:255],
            "error": str(payload.get("error") or "")[:500],
        },
    )
    return JsonResponse({"ok": True})
