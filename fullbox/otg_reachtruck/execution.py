from __future__ import annotations

import copy
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from employees.access import get_employee_for_user
from reachtruck.models import BoxClaim, MoveTask, PalletLock
from reachtruck.services.claims import lock_pallet_for_task, release_claims_for_task
from reachtruck.services.move_requests import sync_task_status_by_legacy_order_id
from reachtruck.services import task_commands as shared_execution
from sku.models import SKUBarcode

from .assignments import shipping_driver_command, order_for_task, assignment_snapshots, assignment_error

from .reserve_swap import OtgReserveSwapResult, prepare_otg_box_scan


MoveTaskCommandResult = shared_execution.MoveTaskCommandResult

_OTG_FINAL_TASK_STATUSES = {
    MoveTask.STATUS_DONE,
    MoveTask.STATUS_CANCELED,
    MoveTask.STATUS_FAILED,
}

_OTG_REQUEST_HIDDEN_STATUSES = {
    MoveTask.STATUS_CANCELED,
    MoveTask.STATUS_FAILED,
}

_OTG_ASSIGNMENT_RELEASE_ROLES = {
    "manager",
    "storekeeper",
    "processing_head",
    "head_manager",
    "director",
    "admin",
}
_OTG_ASSIGNMENT_RELEASE_AFTER = timedelta(minutes=30)


def _normalize_legacy_order_ids(legacy_order_ids) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_value in legacy_order_ids or []:
        value = str(raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _canonicalize_otg_unit_scan(
    payload: dict,
    scan_value: str,
    *,
    agency_id: int | None = None,
) -> str:
    """Return the task barcode for a safe case or same-client SKU alias match."""
    scan_code = str(scan_value or "").strip()
    if not scan_code:
        return scan_code

    candidates: list[str] = []
    seen: set[str] = set()

    def add(value) -> None:
        barcode = str(value or "").strip()
        if not barcode or barcode in seen:
            return
        seen.add(barcode)
        candidates.append(barcode)

    def add_barcode_qty(value) -> None:
        if not isinstance(value, dict):
            return
        for barcode in value:
            add(barcode)

    for barcode in payload.get("requested_barcodes") or []:
        add(barcode)
    add_barcode_qty(payload.get("requested_barcode_qty"))

    for row in payload.get("requested_rows") or []:
        if isinstance(row, dict):
            add_barcode_qty(row.get("barcode_qty"))

    for key in ("partial_pick_patterns", "requested_box_patterns"):
        for pattern in payload.get(key) or []:
            if not isinstance(pattern, dict):
                continue
            for barcode in pattern.get("requested_barcodes") or []:
                add(barcode)
            add_barcode_qty(pattern.get("barcode_qty"))
            add_barcode_qty(pattern.get("source_barcode_qty"))

    for row in payload.get("otg_box_composition") or []:
        if not isinstance(row, dict):
            continue
        add(row.get("barcode"))
        add_barcode_qty(row.get("barcode_qty"))

    matches = [
        candidate
        for candidate in candidates
        if candidate.casefold() == scan_code.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if matches or not agency_id:
        return scan_code

    requested_sku = str(payload.get("requested_sku") or "").strip()
    if not requested_sku or len(candidates) != 1:
        return scan_code

    scanned_sku_codes = list(
        SKUBarcode.objects.filter(
            value=scan_code,
            agency_id=agency_id,
            sku__agency_id=agency_id,
            sku__deleted=False,
        )
        .values_list("sku__sku_code", flat=True)
        .distinct()[:2]
    )
    if scanned_sku_codes == [requested_sku]:
        return candidates[0]
    return scan_code


def _canonicalize_otg_task_unit_scan(
    legacy_order_id: str,
    scan_value: str,
    *,
    current_step: str | None = None,
) -> str:
    step = str(current_step or "").strip()
    if not step:
        snapshot = shared_execution.build_mobile_execution_snapshot(legacy_order_id)
        step = str(snapshot.get("current_step") or "").strip()
    if step != "units":
        return str(scan_value or "").strip()
    task = _load_live_otg_task(legacy_order_id)
    return _canonicalize_otg_unit_scan(
        dict(task.payload or {}) if task is not None else {},
        scan_value,
        agency_id=(
            int(task.request.agency_id or 0)
            if task is not None and task.request_id
            else None
        ),
    )


def _otg_request_visible_legacy_order_ids(legacy_order_ids) -> list[str]:
    normalized_ids = _normalize_legacy_order_ids(legacy_order_ids)
    if not normalized_ids:
        return []
    rows = MoveTask.objects.filter(legacy_order_id__in=normalized_ids).order_by(
        "created_at", "id"
    )
    occurrence_count: dict[str, int] = {}
    visible: set[str] = set()
    for task in rows:
        legacy_id = str(task.legacy_order_id or "").strip()
        if not legacy_id:
            continue
        occurrence_count[legacy_id] = occurrence_count.get(legacy_id, 0) + 1
        if task.status not in _OTG_REQUEST_HIDDEN_STATUSES:
            visible.add(legacy_id)
    # Shared mobile commands address a task only by legacy_order_id and cannot
    # safely disambiguate even when one duplicate is already final/cancelled.
    return [
        legacy_id
        for legacy_id in normalized_ids
        if legacy_id in visible and occurrence_count.get(legacy_id) == 1
    ]


def _load_live_otg_task(legacy_order_id: str, *, for_update: bool = False) -> MoveTask | None:
    target_id = str(legacy_order_id or "").strip()
    if not target_id:
        return None
    qs = (
        MoveTask.objects.select_related("request", "request__agency")
        .filter(legacy_order_id=target_id)
        .exclude(status__in=_OTG_FINAL_TASK_STATUSES)
    )
    if for_update:
        qs = qs.select_for_update(of=("self",))
    matches = list(qs.order_by("-updated_at", "-id")[:2])
    # Fail closed on legacy identifier collisions. Selecting the newest row
    # could silently switch the driver to another client's pallet.
    return matches[0] if len(matches) == 1 else None


def _load_live_otg_tasks(legacy_order_ids, *, for_update: bool = False) -> list[MoveTask]:
    normalized_ids = _normalize_legacy_order_ids(legacy_order_ids)
    if not normalized_ids:
        return []
    qs = (
        MoveTask.objects.select_related("request", "request__agency")
        .filter(legacy_order_id__in=normalized_ids)
        .exclude(status__in=_OTG_FINAL_TASK_STATUSES)
        .order_by("-updated_at", "-id")
    )
    if for_update:
        qs = qs.select_for_update(of=("self",))
    tasks_by_legacy_id: dict[str, MoveTask] = {}
    ambiguous_legacy_ids: set[str] = set()
    for task in qs:
        legacy_order_id = str(task.legacy_order_id or "").strip()
        if not legacy_order_id:
            continue
        if legacy_order_id in tasks_by_legacy_id:
            ambiguous_legacy_ids.add(legacy_order_id)
            continue
        tasks_by_legacy_id[legacy_order_id] = task
    return [
        tasks_by_legacy_id[legacy_order_id]
        for legacy_order_id in normalized_ids
        if legacy_order_id in tasks_by_legacy_id and legacy_order_id not in ambiguous_legacy_ids
    ]


def _otg_task_status(task: MoveTask, payload: dict) -> str:
    return shared_execution._task_payload_status(task, payload)


def _otg_task_assignee_id(task: MoveTask, payload: dict) -> int | None:
    return shared_execution._task_assignee_id(task, payload)


def _otg_task_assignee_employee_id(task: MoveTask, payload: dict) -> int | None:
    raw_assignee = (payload or {}).get("assigned_to_id")
    if raw_assignee not in (None, ""):
        try:
            return int(raw_assignee)
        except (TypeError, ValueError):
            return None
    assigned_user = getattr(task, "assigned_to", None)
    if assigned_user is not None:
        try:
            from employees.access import get_employee_for_user

            employee = get_employee_for_user(assigned_user)
        except Exception:
            employee = None
        employee_id = getattr(employee, "id", None)
        if employee_id:
            try:
                return int(employee_id)
            except (TypeError, ValueError):
                return None
    return None


def _otg_task_has_protected_scan_facts(payload: dict) -> bool:
    execution = dict((payload or {}).get("mobile_execution") or {})
    return bool(
        execution.get("pallet_confirmed")
        or execution.get("boxes_scanned")
        or execution.get("units_scanned")
        or execution.get("marking_scans")
        or execution.get("pending_marking_scan")
        or execution.get("destination_confirmed")
    )


def otg_task_assignment_release_snapshot(
    task: MoveTask,
    *,
    now=None,
) -> dict:
    payload = dict(task.payload or {})
    current_time = now or timezone.now()
    assigned_at = task.started_at or task.updated_at or task.created_at
    idle_seconds = max((current_time - assigned_at).total_seconds(), 0)
    idle_minutes = int(idle_seconds // 60)
    has_scan_facts = _otg_task_has_protected_scan_facts(payload)
    has_claims = task.box_claims.filter(status=BoxClaim.STATUS_CLAIMED).exists()
    has_lock = task.pallet_locks.filter(status=PalletLock.STATUS_ACTIVE).exists()
    is_in_progress = _otg_task_status(task, payload) == MoveTask.STATUS_IN_PROGRESS
    is_stale = idle_seconds >= _OTG_ASSIGNMENT_RELEASE_AFTER.total_seconds()
    return {
        "is_in_progress": is_in_progress,
        "is_stale": is_stale,
        "idle_minutes": idle_minutes,
        "has_scan_facts": has_scan_facts,
        "has_claims": has_claims,
        "has_lock": has_lock,
        "can_release": bool(
            is_in_progress
            and is_stale
            and not has_scan_facts
            and not has_claims
            and not has_lock
        ),
    }


def _active_otg_task_for_request(
    task: MoveTask,
    *,
    employee_id: int | None,
    exclude_task_id: int | None = None,
) -> MoveTask | None:
    if not employee_id or not getattr(task, "request_id", None):
        return None
    qs = (
        MoveTask.objects.select_for_update(of=("self",))
        .filter(request_id=task.request_id, status=MoveTask.STATUS_IN_PROGRESS)
        .exclude(status__in=_OTG_FINAL_TASK_STATUSES)
        .order_by("-updated_at", "-id")
    )
    if exclude_task_id:
        qs = qs.exclude(id=exclude_task_id)
    for candidate in qs:
        payload = dict(candidate.payload or {})
        if _otg_task_assignee_employee_id(candidate, payload) != employee_id:
            continue
        execution = dict(payload.get("mobile_execution") or {})
        if execution.get("destination_confirmed"):
            continue
        if _otg_task_temporarily_blocked_by_foreign_claim(candidate, payload):
            continue
        return candidate
    return None


def _otg_payload_selected_box_codes(payload: dict) -> list[str]:
    if hasattr(shared_execution, "_otg_payload_selected_box_codes"):
        return shared_execution._otg_payload_selected_box_codes(payload)
    if hasattr(shared_execution, "_otg_payload_explicit_box_codes"):
        return shared_execution._otg_payload_explicit_box_codes(payload)
    if not isinstance(payload, dict):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for key in ("planned_box_codes", "selected_box_codes", "reserved_box_codes", "requested_boxes"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for raw_code in values:
            code = str(raw_code or "").strip()
            code_key = code.lower()
            if not code or code_key in seen:
                continue
            seen.add(code_key)
            result.append(code)
        if result:
            return result
    return result


def _otg_current_source_runtime_payload(task: MoveTask, payload: dict) -> tuple[dict, str]:
    placement_payload = shared_execution._cached_mobile_placement_payload(payload)
    pallet_code = str(payload.get("pallet_code") or task.pallet_code or "").strip()
    if not pallet_code:
        return payload, ""
    if not placement_payload:
        placement_entry = shared_execution._find_placement_entry_for_pallet(
            pallet_code,
            receiving_order_id=str(payload.get("receiving_order_id") or "").strip() or None,
            processing_order_id=str(payload.get("processing_order_id") or "").strip() or None,
            agency_id=int(task.request.agency_id) if task.request and task.request.agency_id else None,
        )
        if not placement_entry:
            return payload, "Паллета не найдена в размещении."
        payload, placement_payload = shared_execution._cache_mobile_placement_snapshot(
            task,
            payload,
            placement_entry,
        )

    to_zone = shared_execution._normalize_zone_code((payload.get("to_location") or {}).get("zone") or "")
    if (
        to_zone == "OTG"
        and not shared_execution._uses_otg_scan_fact_mode(payload)
        and not shared_execution._pallet_choice_pending(payload)
    ):
        shared_execution._adapt_flexible_payload_to_selected_pallet(
            payload,
            placement_payload,
            pallet_code,
        )
    payload = shared_execution._mobile_sync_execution_payload(task, payload, placement_payload)
    return payload, ""


def _active_otg_box_claims(
    tasks: list[MoveTask],
    *,
    for_update: bool = False,
) -> list[BoxClaim]:
    agency_ids = {
        int(task.request.agency_id)
        for task in tasks
        if task.request and task.request.agency_id
    }
    if not agency_ids:
        return []
    qs = (
        BoxClaim.objects.select_related("move_task")
        .filter(
            agency_id__in=agency_ids,
            status=BoxClaim.STATUS_CLAIMED,
        )
        .exclude(move_task__status__in=_OTG_FINAL_TASK_STATUSES)
        .order_by("id")
    )
    if for_update:
        qs = qs.select_for_update(of=("self",))
    return list(qs)


def _otg_task_label(task: MoveTask | None) -> str:
    if task is None:
        return ""
    legacy_order_id = str(task.legacy_order_id or "").strip()
    return legacy_order_id or str(task.id or "").strip()


def _active_otg_pallet_blocker_message(task: MoveTask, payload: dict) -> str:
    pallet_code = str(payload.get("pallet_code") or task.pallet_code or "").strip()
    agency_id = int(task.request.agency_id) if task.request and task.request.agency_id else None
    if not pallet_code or not agency_id:
        return ""

    active_lock = (
        PalletLock.objects.select_for_update(of=("self",))
        .select_related("move_task")
        .filter(
            agency_id=agency_id,
            pallet_code=pallet_code,
            status=PalletLock.STATUS_ACTIVE,
        )
        .exclude(move_task_id=task.id)
        .exclude(move_task__status__in=_OTG_FINAL_TASK_STATUSES)
        .order_by("id")
        .first()
    )
    if active_lock is not None:
        label = _otg_task_label(active_lock.move_task)
        suffix = f" заданием {label}" if label else " другим заданием"
        return f"Паллета {pallet_code} уже занята{suffix}. Обновите экран или завершите активную паллету."

    active_task = (
        MoveTask.objects.select_for_update(of=("self",))
        .filter(
            Q(pallet_code=pallet_code) | Q(payload__pallet_code=pallet_code),
            request__agency_id=agency_id,
            status=MoveTask.STATUS_IN_PROGRESS,
        )
        .exclude(id=task.id)
        .exclude(status__in=_OTG_FINAL_TASK_STATUSES)
        .order_by("-updated_at", "-id")
        .first()
    )
    if active_task is not None:
        label = _otg_task_label(active_task)
        suffix = f" заданием {label}" if label else " другим заданием"
        return f"Паллета {pallet_code} уже занята{suffix}. Повторное действие не выполнено."
    return ""


def _otg_task_temporarily_blocked_by_foreign_claim(
    task: MoveTask,
    payload: dict | None = None,
    *,
    active_claims: list[BoxClaim] | None = None,
    stock_boxes_cache: dict[tuple[int | None, str], list[dict]] | None = None,
) -> bool:
    runtime_payload = dict(payload or task.payload or {})
    if _otg_task_status(task, runtime_payload) not in {
        MoveTask.STATUS_CREATED,
        MoveTask.STATUS_IN_PROGRESS,
    }:
        return False
    execution = dict(runtime_payload.get("mobile_execution") or {})
    if execution.get("destination_confirmed"):
        return False
    if execution.get("boxes_scanned") or execution.get("units_scanned"):
        return False

    requested_count = shared_execution._requested_box_count(runtime_payload)
    pallet_code = str(runtime_payload.get("pallet_code") or task.pallet_code or "").strip()
    agency_id = int(task.request.agency_id) if task.request and task.request.agency_id else None
    if requested_count <= 0 or not pallet_code or not agency_id:
        return False

    claims = active_claims
    if claims is None:
        claims = _active_otg_box_claims([task])
    foreign_claim_keys = {
        str(claim.box_code or "").strip().lower()
        for claim in claims
        if claim.agency_id == agency_id
        and claim.move_task_id != task.id
        and str(claim.box_code or "").strip()
    }
    if not foreign_claim_keys:
        return False

    selected_without_claims, _matching_without_claims = (
        shared_execution._otg_selected_stock_boxes_for_payload(
            runtime_payload,
            pallet_code,
            agency_id=agency_id,
            claimed_box_keys=set(),
            stock_boxes_cache=stock_boxes_cache,
        )
    )
    if len(selected_without_claims) < requested_count:
        return False
    selected_available, _matching_available = (
        shared_execution._otg_selected_stock_boxes_for_payload(
            runtime_payload,
            pallet_code,
            agency_id=agency_id,
            claimed_box_keys=foreign_claim_keys,
            stock_boxes_cache=stock_boxes_cache,
        )
    )
    return (
        len(selected_available) < requested_count
        and any(
            str(code or "").strip().lower() in foreign_claim_keys
            for code in selected_without_claims
        )
    )


def _select_otg_request_task(
    legacy_order_ids,
    *,
    employee_id: int | None,
    for_update: bool = False,
) -> tuple[MoveTask | None, list[str]]:
    tasks = _load_live_otg_tasks(
        _otg_request_visible_legacy_order_ids(legacy_order_ids),
        for_update=for_update,
    )
    if not tasks:
        return None, []

    active_claims = _active_otg_box_claims(tasks, for_update=for_update)
    stock_boxes_cache: dict[tuple[int | None, str], list[dict]] = {}
    temporarily_skipped: list[str] = []
    active_candidates: list[tuple[MoveTask, dict]] = []
    queued_candidates: list[tuple[MoveTask, dict]] = []
    for task in tasks:
        payload = dict(task.payload or {})
        status = _otg_task_status(task, payload)
        execution = dict(payload.get("mobile_execution") or {})
        if status in _OTG_FINAL_TASK_STATUSES or execution.get("destination_confirmed"):
            continue
        assigned_employee_id = _otg_task_assignee_employee_id(task, payload)
        if status == MoveTask.STATUS_IN_PROGRESS:
            if assigned_employee_id and employee_id is not None and assigned_employee_id != employee_id:
                continue
            active_candidates.append((task, payload))
            continue
        queued_candidates.append((task, payload))

    if active_candidates:
        active_candidates.sort(
            key=lambda item: (item[0].updated_at, item[0].id),
            reverse=True,
        )
        for task, payload in active_candidates:
            if _otg_task_temporarily_blocked_by_foreign_claim(
                task,
                payload,
                active_claims=active_claims,
                stock_boxes_cache=stock_boxes_cache,
            ):
                temporarily_skipped.append(str(task.legacy_order_id or "").strip())
                continue
            return task, temporarily_skipped

    for task, payload in queued_candidates:
        if _otg_task_temporarily_blocked_by_foreign_claim(
            task,
            payload,
            active_claims=active_claims,
            stock_boxes_cache=stock_boxes_cache,
        ):
            temporarily_skipped.append(str(task.legacy_order_id or "").strip())
            continue
        return task, temporarily_skipped
    return None, temporarily_skipped


def _verify_or_retarget_otg_source(task: MoveTask, payload: dict) -> tuple[dict, bool, str]:
    if shared_execution._pallet_choice_pending(payload):
        return payload, False, ""
    if shared_execution._uses_otg_scan_fact_mode(payload):
        payload, error = _otg_current_source_runtime_payload(task, payload)
        return payload, False, error
    if not shared_execution._otg_task_needs_live_source_refresh(payload):
        payload, error = _otg_current_source_runtime_payload(task, payload)
        return payload, False, error

    execution = dict(payload.get("mobile_execution") or {})
    if execution.get("boxes_scanned") or execution.get("units_scanned"):
        payload, error = _otg_current_source_runtime_payload(task, payload)
        return payload, False, error

    current_pallet = str(payload.get("pallet_code") or task.pallet_code or "").strip()
    requested_count = shared_execution._requested_box_count(payload)
    agency_id = int(task.request.agency_id) if task.request and task.request.agency_id else None
    claimed_box_keys = {
        str(code or "").strip().lower()
        for code in shared_execution.active_box_claim_codes(agency_id=agency_id)
        if str(code or "").strip()
    }

    if current_pallet and requested_count > 0:
        selected_codes, _matching_count = shared_execution._otg_selected_stock_boxes_for_payload(
            payload,
            current_pallet,
            agency_id=agency_id,
            claimed_box_keys=claimed_box_keys,
        )
        if len(selected_codes) >= requested_count:
            normalized_selected = selected_codes[:requested_count]
            previous_selected = _otg_payload_selected_box_codes(payload)
            changed = previous_selected != normalized_selected
            payload["planned_box_codes"] = normalized_selected
            payload["selected_box_codes"] = normalized_selected
            payload["reserved_box_codes"] = normalized_selected
            payload["otg_live_source_verified_at"] = timezone.localtime().isoformat()
            payload, error = _otg_current_source_runtime_payload(task, payload)
            if error:
                return payload, changed, error
            shared_execution._sync_otg_plan_live_pallet(payload, normalized_selected)
            shared_execution._save_task_source_payload(task, payload)
            return payload, changed, ""

    payload, _placement_payload, _placement_source, refreshed, refresh_error = (
        shared_execution._refresh_otg_live_source_for_task(
            task,
            payload,
            allow_partial_current=True,
        )
    )
    if refresh_error:
        return payload, refreshed, refresh_error
    payload, runtime_error = _otg_current_source_runtime_payload(task, payload)
    if runtime_error:
        return payload, refreshed, runtime_error
    if refreshed:
        shared_execution._save_task_source_payload(task, payload)
    return payload, refreshed, ""


def _take_loaded_otg_move_task(
    task: MoveTask,
    *,
    user,
    employee_id: int | None,
    employee_name: str,
    request_batch_mode: bool = False,
) -> MoveTaskCommandResult:
    payload = dict(task.payload or {})
    status = _otg_task_status(task, payload)
    if status in _OTG_FINAL_TASK_STATUSES:
        return MoveTaskCommandResult(ok=False, error="Задание уже закрыто.")
    assigned_employee_id = _otg_task_assignee_employee_id(task, payload)
    if status == MoveTask.STATUS_IN_PROGRESS and (
        not assigned_employee_id or assigned_employee_id == employee_id
    ):
        pallet_code = str(payload.get("pallet_code") or task.pallet_code or "").strip()
        return MoveTaskCommandResult(
            ok=False,
            error=(
                f"Паллета {pallet_code} уже взята в работу. "
                "Продолжайте сканирование, повторно брать задание не нужно."
            ),
        )
    active_task = _active_otg_task_for_request(
        task,
        employee_id=employee_id,
        exclude_task_id=task.id,
    )
    if active_task is not None and status != MoveTask.STATUS_IN_PROGRESS:
        return MoveTaskCommandResult(
            ok=False,
            error=(
                "\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0437\u0430\u043a\u0440\u043e\u0439\u0442\u0435 "
                f"\u0430\u043a\u0442\u0438\u0432\u043d\u0443\u044e \u043f\u0430\u043b\u043b\u0435\u0442\u0443 "
                f"{active_task.pallet_code or ''} \u043f\u043e \u044d\u0442\u043e\u0439 OTG-\u0437\u0430\u044f\u0432\u043a\u0435."
            ),
        )

    payload, _source_changed, source_error = _verify_or_retarget_otg_source(task, payload)
    if source_error:
        return MoveTaskCommandResult(ok=False, error=source_error)
    blocker_message = _active_otg_pallet_blocker_message(task, payload)
    if blocker_message:
        return MoveTaskCommandResult(ok=False, error=blocker_message)
    if (
        not shared_execution._uses_otg_scan_fact_mode(payload)
        and not shared_execution._pallet_choice_pending(payload)
    ):
        try:
            lock_pallet_for_task(
                task,
                locked_by=user,
                payload={
                    "legacy_order_id": task.legacy_order_id,
                    "shipping_order_id": str(payload.get("shipping_order_id") or "").strip(),
                    "locked_on": "take_otg_move_task",
                },
            )
        except ValueError as exc:
            return MoveTaskCommandResult(ok=False, error=str(exc))

    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    payload["status"] = MoveTask.STATUS_IN_PROGRESS
    payload["status_label"] = "В работе"
    payload["assigned_to_id"] = employee_id
    payload["assigned_to_name"] = employee_name
    payload["taken_at"] = timezone.localtime().isoformat()
    if request_batch_mode:
        payload["mobile_request_batch_mode"] = True

    task.status = MoveTask.STATUS_IN_PROGRESS
    task.assigned_to = authenticated_user
    task.assigned_to_name = employee_name
    task.started_at = task.started_at or timezone.now()
    task.payload = payload
    task.save(
        update_fields=[
            "status",
            "assigned_to",
            "assigned_to_name",
            "started_at",
            "payload",
            "updated_at",
        ]
    )
    sync_task_status_by_legacy_order_id(
        task.legacy_order_id,
        status=MoveTask.STATUS_IN_PROGRESS,
        assigned_to=authenticated_user,
        assigned_to_name=employee_name,
    )
    task.refresh_from_db()
    task.status = MoveTask.STATUS_IN_PROGRESS
    task.assigned_to = authenticated_user
    task.assigned_to_name = employee_name
    task.started_at = task.started_at or timezone.now()
    task.payload = payload
    task.save(
        update_fields=[
            "status",
            "assigned_to",
            "assigned_to_name",
            "started_at",
            "payload",
            "updated_at",
        ]
    )
    return MoveTaskCommandResult(ok=True, task=task, payload=payload, message="Задание взято в работу.")


def _otg_box_pattern_key(pattern: dict) -> tuple:
    barcode_qty = shared_execution._normalize_barcode_qty_map(
        (pattern or {}).get("barcode_qty")
    )
    requested_barcodes = tuple(
        sorted(
            str(value).strip()
            for value in ((pattern or {}).get("requested_barcodes") or [])
            if str(value or "").strip()
        )
    )
    return (
        shared_execution._as_int((pattern or {}).get("box_qty")),
        tuple(sorted(barcode_qty.items())),
        str((pattern or {}).get("requested_article") or "").strip().casefold(),
        shared_execution._normalize_goods_type(
            (pattern or {}).get("requested_goods_type")
        ),
        requested_barcodes,
    )


def _otg_remaining_box_patterns(task: MoveTask) -> list[dict]:
    fresh_task = (
        MoveTask.objects.select_related("request", "request__agency")
        .filter(pk=task.pk)
        .first()
    )
    if fresh_task is None:
        return []
    payload = dict(fresh_task.payload or {})
    if shared_execution._requested_box_selection_mode(payload) != "pattern_matching":
        return []
    raw_patterns = [
        dict(pattern)
        for pattern in shared_execution._requested_box_patterns(payload)
        if isinstance(pattern, dict)
        and shared_execution._as_int(pattern.get("requested_box_count")) > 0
    ]
    if not raw_patterns:
        return []

    execution = dict(payload.get("mobile_execution") or {})
    scanned_codes: list[str] = []
    seen_codes: set[str] = set()
    for raw_code in execution.get("boxes_scanned") or []:
        box_code = shared_execution._normalize_box_code(raw_code)
        box_key = box_code.casefold()
        if not box_code or box_key in seen_codes:
            continue
        seen_codes.add(box_key)
        scanned_codes.append(box_code)
    if not scanned_codes:
        return raw_patterns

    placement_payload = shared_execution._cached_mobile_placement_payload(payload)
    if not placement_payload:
        shared_execution.build_mobile_execution_snapshot(fresh_task.legacy_order_id)
        fresh_task.refresh_from_db(fields=["payload"])
        payload = dict(fresh_task.payload or {})
        placement_payload = shared_execution._cached_mobile_placement_payload(payload)
    if not placement_payload:
        return []

    selected_codes, consumed_patterns = (
        shared_execution._assign_partial_requested_patterns_to_boxes(
            payload,
            placement_payload,
            str(payload.get("pallet_code") or fresh_task.pallet_code or "").strip(),
            scanned_codes,
        )
    )
    if len(selected_codes) != len(scanned_codes):
        return []

    consumed_counts: dict[tuple, int] = {}
    for pattern in consumed_patterns:
        key = _otg_box_pattern_key(pattern)
        consumed_counts[key] = consumed_counts.get(key, 0) + max(
            shared_execution._as_int(pattern.get("requested_box_count")),
            0,
        )

    remaining: list[dict] = []
    for pattern in raw_patterns:
        key = _otg_box_pattern_key(pattern)
        requested_count = max(
            shared_execution._as_int(pattern.get("requested_box_count")),
            0,
        )
        consumed_count = min(consumed_counts.get(key, 0), requested_count)
        consumed_counts[key] = max(consumed_counts.get(key, 0) - consumed_count, 0)
        remaining_count = requested_count - consumed_count
        if remaining_count <= 0:
            continue
        remaining_pattern = dict(pattern)
        remaining_pattern["requested_box_count"] = remaining_count
        remaining.append(remaining_pattern)
    return remaining


def _otg_remaining_box_summary(task: MoveTask) -> str:
    patterns = _otg_remaining_box_patterns(task)
    if not patterns:
        return ""
    return shared_execution._otg_live_required_summary(
        {"requested_box_patterns": patterns}
    )


def _enrich_otg_box_snapshot(task: MoveTask, snapshot: dict) -> dict:
    result = dict(snapshot or {})
    payload = dict(task.payload or {})
    reports = [
        dict(report)
        for report in (payload.get("unit_shortage_reports") or [])
        if isinstance(report, dict)
    ]
    if reports:
        latest_report = reports[-1]
        result["unit_shortage_notice"] = {
            "available": True,
            "box_code": str(latest_report.get("box_code") or "").strip(),
            "barcode": str(latest_report.get("barcode") or "").strip(),
            "expected_qty": shared_execution._as_int(latest_report.get("expected_qty")),
            "actual_qty": shared_execution._as_int(latest_report.get("actual_qty")),
            "missing_qty": shared_execution._as_int(latest_report.get("missing_qty")),
            "verification_task_id": shared_execution._as_int(
                latest_report.get("verification_task_id")
            ),
        }

    unit_entry = dict(result.get("unit_quantity_entry") or {})
    if (
        str(result.get("current_step") or "").strip() == "units"
        and unit_entry.get("available")
    ):
        box_code = str(unit_entry.get("box_code") or "").strip()
        current_qty = shared_execution._as_int(unit_entry.get("current_qty"))
        required_qty = shared_execution._as_int(unit_entry.get("required_qty"))
        active_box = next(
            (
                dict(row)
                for row in (result.get("boxes") or [])
                if isinstance(row, dict)
                and str(row.get("box_code") or "").strip().casefold()
                == box_code.casefold()
            ),
            {},
        )
        barcode_qty = shared_execution._normalize_barcode_qty_map(
            active_box.get("unit_barcode_qty")
        )
        execution = dict(payload.get("mobile_execution") or {})
        barcode = str(execution.get("last_scan") or "").strip()
        source_box_qty = shared_execution._as_int(active_box.get("box_qty"))
        if (
            box_code
            and barcode
            and len(result.get("boxes") or []) == 1
            and len(barcode_qty) == 1
            and barcode in barcode_qty
            and required_qty > current_qty > 0
            and source_box_qty == required_qty
            and not bool(active_box.get("return_required"))
        ):
            result["unit_shortage_entry"] = {
                "available": True,
                "box_code": box_code,
                "barcode": barcode,
                "current_qty": current_qty,
                "required_qty": required_qty,
                "missing_qty": required_qty - current_qty,
            }

    if (
        str(result.get("current_step") or "").strip() != "boxes"
        or bool(result.get("box_selection_complete"))
    ):
        return result
    summary = _otg_remaining_box_summary(task)
    if not summary:
        return result
    result["remaining_box_summary"] = summary
    result["expected_scan"] = summary
    prompt = str(result.get("prompt") or "").strip()
    hint = f"Осталось подобрать: {summary}."
    result["prompt"] = f"{prompt} {hint}".strip() if hint not in prompt else prompt
    return result


def _append_otg_remaining_box_hint(
    result: MoveTaskCommandResult,
    legacy_order_id: str,
) -> MoveTaskCommandResult:
    if result.ok:
        return result
    error = str(result.error or "").strip()
    normalized_error = error.casefold()
    if "короб" not in normalized_error or not (
        "не подходит" in normalized_error
        or "коробочную схему" in normalized_error
    ):
        return result
    task = _load_live_otg_task(legacy_order_id)
    if task is None:
        return result
    summary = _otg_remaining_box_summary(task)
    if summary and "осталось подобрать:" not in normalized_error:
        result.error = f"{error} Осталось подобрать: {summary}."
    return result


def build_otg_mobile_execution_snapshot(legacy_order_id: str) -> dict:
    if not _otg_request_visible_legacy_order_ids([legacy_order_id]):
        return {}
    with transaction.atomic():
        snapshot = shared_execution.build_mobile_execution_snapshot(legacy_order_id)
        task = _load_live_otg_task(legacy_order_id)
        return _enrich_otg_box_snapshot(task, snapshot) if task is not None else snapshot


def _build_otg_mobile_request_execution_snapshot(
    legacy_order_ids,
    *,
    employee_id: int | None = None,
) -> dict:
    visible_order_ids = _otg_request_visible_legacy_order_ids(legacy_order_ids)
    if not visible_order_ids:
        return {}
    with transaction.atomic():
        snapshot = shared_execution.build_mobile_request_execution_snapshot(
            visible_order_ids,
            employee_id=employee_id,
        )
        selected_task, temporarily_skipped = _select_otg_request_task(
            visible_order_ids,
            employee_id=employee_id,
        )
    snapshot["temporarily_skipped_order_ids"] = temporarily_skipped
    snapshot["temporarily_skipped_count"] = len(temporarily_skipped)
    snapshot["box_claim_blocked"] = bool(temporarily_skipped)
    if selected_task is not None:
        selected_payload = dict(selected_task.payload or {})
        selected_status = _otg_task_status(selected_task, selected_payload)
        assigned_employee_id = _otg_task_assignee_employee_id(
            selected_task,
            selected_payload,
        )
        owned_active = (
            selected_status == MoveTask.STATUS_IN_PROGRESS
            and (
                employee_id is None
                or assigned_employee_id == employee_id
            )
        )
        active_snapshot = _enrich_otg_box_snapshot(
            selected_task,
            shared_execution.build_mobile_execution_snapshot(
                selected_task.legacy_order_id
            ),
        )
        snapshot.update(
            {
                "can_take": not owned_active,
                "can_scan": owned_active,
                "taken_by_other": False,
                "current_step": str(active_snapshot.get("current_step") or "pallet"),
                "prompt": str(active_snapshot.get("prompt") or ""),
                "expected_scan": str(active_snapshot.get("expected_scan") or ""),
                "remaining_box_summary": str(
                    active_snapshot.get("remaining_box_summary") or ""
                ),
                "unit_quantity_entry": dict(
                    active_snapshot.get("unit_quantity_entry") or {}
                ),
                "unit_shortage_entry": dict(
                    active_snapshot.get("unit_shortage_entry") or {}
                ),
                "unit_shortage_notice": dict(
                    active_snapshot.get("unit_shortage_notice") or {}
                ),
                "active_order_id": str(selected_task.legacy_order_id or ""),
                "active_pallet_code": str(
                    active_snapshot.get("pallet_code")
                    or selected_payload.get("pallet_code")
                    or ""
                ),
                "active_destination_code": str(
                    active_snapshot.get("destination_code")
                    or selected_payload.get("destination_code")
                    or ""
                ),
                "active_destination_label": str(
                    active_snapshot.get("destination_label")
                    or selected_payload.get("destination_label")
                    or ""
                ),
            }
        )
        return snapshot
    snapshot.update(
        {
            "can_take": False,
            "can_scan": False,
            "taken_by_other": bool(temporarily_skipped) or bool(snapshot.get("taken_by_other")),
            "active_order_id": "",
            "active_pallet_code": "",
            "active_destination_code": "",
            "active_destination_label": "",
        }
    )
    if temporarily_skipped:
        snapshot["prompt"] = (
            "Подходящие короба временно заняты другим заданием. "
            "После освобождения они автоматически вернутся в очередь."
        )
    return snapshot



def build_otg_mobile_request_execution_snapshot(legacy_order_ids, *, employee_id=None):
    tasks = _load_live_otg_tasks(legacy_order_ids)
    if not tasks:
        return {}
    order = order_for_task(tasks[0])
    state = assignment_snapshots([order])[order.pk] if order is not None else {}
    error = assignment_error(state, employee_id)
    can_finish_existing = state.get("conflict") and employee_id in state.get("employee_ids", [])
    if error and not can_finish_existing:
        # Do not build a foreign driver's execution: the shared legacy snapshot
        # may persist calculated pallet metadata even when invoked for display.
        return {"can_take": False, "can_scan": False, "taken_by_other": True,
                "prompt": error, "assignment_error": error, "assignment": state,
                "active_order_id": ""}
    snapshot = _build_otg_mobile_request_execution_snapshot(legacy_order_ids, employee_id=employee_id)
    if error:
        snapshot["can_take"] = False
        if not snapshot.get("can_scan"):
            snapshot["taken_by_other"] = True
            snapshot["prompt"] = error
        snapshot["assignment_error"] = error
    snapshot["assignment"] = state
    return snapshot


@shipping_driver_command(taking=True)
def take_otg_move_request(
    legacy_order_ids,
    *,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    visible_order_ids = _otg_request_visible_legacy_order_ids(legacy_order_ids)
    if not visible_order_ids:
        return MoveTaskCommandResult(ok=False, error="Живые задания OTG не найдены.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="Профиль сотрудника не найден.")

    with transaction.atomic():
        task_to_take, temporarily_skipped = _select_otg_request_task(
            visible_order_ids,
            employee_id=employee_id,
            for_update=True,
        )
        if task_to_take is None:
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    "Подходящие короба временно заняты другим заданием."
                    if temporarily_skipped
                    else "Доступные задания OTG не найдены."
                ),
            )
        result = _take_loaded_otg_move_task(
            task_to_take,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
            request_batch_mode=True,
        )
        if not result.ok:
            transaction.set_rollback(True)
            return result
        return MoveTaskCommandResult(
            ok=True,
            task=result.task,
            payload=result.payload,
            message="Задание заявки взято в работу.",
        )


@shipping_driver_command(taking=True)
def take_selected_otg_move_request_task(
    legacy_order_ids,
    *,
    selected_legacy_order_id: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    visible_order_ids = _otg_request_visible_legacy_order_ids(legacy_order_ids)
    selected_id = str(selected_legacy_order_id or "").strip()
    if not visible_order_ids:
        return MoveTaskCommandResult(ok=False, error="Живые задания OTG не найдены.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="Профиль сотрудника не найден.")
    if not selected_id or selected_id not in visible_order_ids:
        return MoveTaskCommandResult(
            ok=False,
            error="Выбранная паллета не относится к этой OTG-заявке.",
        )

    with transaction.atomic():
        task = _load_live_otg_task(selected_id, for_update=True)
        if task is None:
            return MoveTaskCommandResult(ok=False, error="Выбранное задание OTG уже закрыто.")
        payload = dict(task.payload or {})
        status = _otg_task_status(task, payload)
        assigned_employee_id = _otg_task_assignee_employee_id(task, payload)
        if (
            status == MoveTask.STATUS_IN_PROGRESS
            and assigned_employee_id
            and assigned_employee_id != employee_id
        ):
            return MoveTaskCommandResult(
                ok=False,
                error="Эта паллета уже взята в работу другим водителем.",
            )
        active_claims = _active_otg_box_claims([task], for_update=True)
        if _otg_task_temporarily_blocked_by_foreign_claim(
            task,
            payload,
            active_claims=active_claims,
        ):
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    "Подходящие короба на этой паллете временно заняты другим "
                    "заданием. Выберите другую паллету."
                ),
            )
        result = _take_loaded_otg_move_task(
            task,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
            request_batch_mode=True,
        )
        if not result.ok:
            transaction.set_rollback(True)
            return result
        return MoveTaskCommandResult(
            ok=True,
            task=result.task,
            payload=result.payload,
            message="Выбранная паллета взята в работу.",
        )


def release_stale_otg_move_request_task(
    legacy_order_ids,
    *,
    selected_legacy_order_id: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    employee = get_employee_for_user(user)
    if employee is None or employee.role not in _OTG_ASSIGNMENT_RELEASE_ROLES:
        return MoveTaskCommandResult(
            ok=False,
            error="Вернуть задание в очередь может только руководитель.",
        )
    if not employee_id or int(employee.id) != int(employee_id):
        return MoveTaskCommandResult(ok=False, error="Профиль сотрудника не найден.")

    visible_order_ids = _otg_request_visible_legacy_order_ids(legacy_order_ids)
    selected_id = str(selected_legacy_order_id or "").strip()
    if not selected_id or selected_id not in visible_order_ids:
        return MoveTaskCommandResult(
            ok=False,
            error="Выбранная паллета не относится к этой OTG-заявке.",
        )

    with transaction.atomic():
        task = _load_live_otg_task(selected_id, for_update=True)
        if task is None:
            return MoveTaskCommandResult(ok=False, error="Активное задание OTG не найдено.")
        payload = dict(task.payload or {})
        release_snapshot = otg_task_assignment_release_snapshot(task)
        if not release_snapshot["is_in_progress"]:
            return MoveTaskCommandResult(ok=False, error="Задание уже находится в очереди.")
        if not release_snapshot["is_stale"]:
            return MoveTaskCommandResult(
                ok=False,
                error="Вернуть задание можно после 30 минут бездействия.",
            )
        if release_snapshot["has_scan_facts"]:
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    "По заданию уже есть сканы. Чтобы исключить двойное перемещение, "
                    "возврат в очередь запрещён."
                ),
            )
        if release_snapshot["has_claims"] or release_snapshot["has_lock"]:
            return MoveTaskCommandResult(
                ok=False,
                error="По заданию есть активные резервы коробов или паллеты.",
            )

        previous_assignee = str(
            task.assigned_to_name or payload.get("assigned_to_name") or ""
        ).strip()
        release_history = [
            dict(entry)
            for entry in (payload.get("assignment_release_history") or [])
            if isinstance(entry, dict)
        ][-19:]
        release_history.append(
            {
                "released_at": timezone.localtime().isoformat(),
                "released_by_id": int(employee.id),
                "released_by_name": employee_name,
                "released_by_role": employee.role,
                "previous_assignee": previous_assignee,
                "idle_minutes": int(release_snapshot["idle_minutes"] or 0),
            }
        )

        sync_task_status_by_legacy_order_id(
            task.legacy_order_id,
            status=MoveTask.STATUS_CREATED,
        )
        task.refresh_from_db()
        payload = dict(task.payload or {})
        execution = dict(payload.get("mobile_execution") or {})
        for field_name in (
            "source_confirmed",
            "pallet_confirmed",
            "last_scan",
        ):
            execution.pop(field_name, None)
        payload["mobile_execution"] = execution
        payload["status"] = MoveTask.STATUS_CREATED
        payload["status_label"] = "Ожидает водителя"
        payload["assignment_release_history"] = release_history
        for field_name in (
            "assigned_to_id",
            "assigned_to_name",
            "taken_at",
            "mobile_request_batch_mode",
        ):
            payload.pop(field_name, None)

        task.status = MoveTask.STATUS_CREATED
        task.assigned_to = None
        task.assigned_to_name = ""
        task.started_at = None
        task.payload = payload
        task.save(
            update_fields=[
                "status",
                "assigned_to",
                "assigned_to_name",
                "started_at",
                "payload",
                "updated_at",
            ]
        )
        release_claims_for_task(task, delivered=False)
        return MoveTaskCommandResult(
            ok=True,
            task=task,
            payload=payload,
            message=(
                f"Задание возвращено в очередь. Предыдущий водитель: {previous_assignee}."
                if previous_assignee
                else "Задание возвращено в очередь."
            ),
        )


@shipping_driver_command()
def confirm_otg_move_request_destination_override(
    legacy_order_ids,
    *,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    visible_order_ids = _otg_request_visible_legacy_order_ids(legacy_order_ids)
    if not visible_order_ids:
        return MoveTaskCommandResult(ok=False, error="Живые задания OTG не найдены.")
    with transaction.atomic():
        execution = build_otg_mobile_request_execution_snapshot(
            visible_order_ids,
            employee_id=employee_id,
        )
        active_order_id = str(execution.get("active_order_id") or "").strip()
        if not active_order_id or not execution.get("can_scan"):
            return MoveTaskCommandResult(ok=False, error="Нет активного задания для подтверждения.")
        _adopt_active_otg_task(
            active_order_id,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        return shared_execution.confirm_move_request_destination_override(
            legacy_order_ids=[active_order_id],
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )


def _adopt_active_otg_task(
    legacy_order_id: str,
    *,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTask | None:
    if not employee_id:
        return None
    task = _load_live_otg_task(legacy_order_id, for_update=True)
    if task is None:
        return None
    payload = dict(task.payload or {})
    if _otg_task_status(task, payload) in _OTG_FINAL_TASK_STATUSES:
        return task

    payload["assigned_to_id"] = employee_id
    payload["assigned_to_name"] = employee_name
    task.payload = payload
    task.assigned_to_name = employee_name
    update_fields = ["payload", "assigned_to_name", "updated_at"]
    if getattr(user, "is_authenticated", False):
        task.assigned_to = user
        update_fields.append("assigned_to")
    task.save(update_fields=update_fields)
    return task


@shipping_driver_command()
def scan_otg_move_request_step(
    legacy_order_ids,
    *,
    scan_value: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    visible_order_ids = _otg_request_visible_legacy_order_ids(legacy_order_ids)
    if not visible_order_ids:
        return MoveTaskCommandResult(ok=False, error="Живые задания OTG не найдены.")
    with transaction.atomic():
        execution_snapshot = build_otg_mobile_request_execution_snapshot(
            visible_order_ids,
            employee_id=employee_id,
        )
        active_order_id = str(execution_snapshot.get("active_order_id") or "").strip()
        active_step = str(execution_snapshot.get("current_step") or "").strip()
        if not active_order_id or not execution_snapshot.get("can_scan"):
            return MoveTaskCommandResult(
                ok=False,
                error=(
                    "Сначала возьмите доступное задание в работу."
                    if execution_snapshot.get("can_take")
                    else "Подходящие короба временно заняты другим заданием."
                ),
            )
        _adopt_active_otg_task(
            active_order_id,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        if active_order_id and active_step != "boxes":
            return shared_execution.scan_move_task_step(
                legacy_order_id=active_order_id,
                scan_value=_canonicalize_otg_task_unit_scan(
                    active_order_id,
                    scan_value,
                    current_step=active_step,
                ),
                user=user,
                employee_id=employee_id,
                employee_name=employee_name,
            )
        prepared = _prepare_active_request_box_scan(
            visible_order_ids,
            scan_value=scan_value,
            user=user,
            employee_id=employee_id,
        )
        if not prepared.ok:
            failed_result = _append_otg_remaining_box_hint(
                MoveTaskCommandResult(ok=False, error=prepared.error),
                active_order_id,
            )
            transaction.set_rollback(True)
            return failed_result
        result = shared_execution.scan_move_task_step(
            legacy_order_id=active_order_id,
            scan_value=scan_value,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        result = _append_otg_remaining_box_hint(result, active_order_id)
        if prepared.changed and not result.ok:
            transaction.set_rollback(True)
        return result


@shipping_driver_command(taking=True)
def take_otg_move_task(
    *,
    legacy_order_id: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="Профиль сотрудника не найден.")
    with transaction.atomic():
        task = _load_live_otg_task(legacy_order_id, for_update=True)
        if not task:
            return MoveTaskCommandResult(ok=False, error="Живое задание OTG не найдено.")
        return _take_loaded_otg_move_task(
            task,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )


@shipping_driver_command()
def scan_otg_move_task_step(
    *,
    legacy_order_id: str,
    scan_value: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    with transaction.atomic():
        _adopt_active_otg_task(
            legacy_order_id,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        prepared = prepare_otg_box_scan(
            legacy_order_id=legacy_order_id,
            scan_value=scan_value,
            user=user,
        )
        if not prepared.ok:
            failed_result = _append_otg_remaining_box_hint(
                MoveTaskCommandResult(ok=False, error=prepared.error),
                legacy_order_id,
            )
            transaction.set_rollback(True)
            return failed_result
        result = shared_execution.scan_move_task_step(
            legacy_order_id=legacy_order_id,
            scan_value=_canonicalize_otg_task_unit_scan(
                legacy_order_id,
                scan_value,
            ),
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        result = _append_otg_remaining_box_hint(result, legacy_order_id)
        if prepared.changed and not result.ok:
            transaction.set_rollback(True)
        return result


@shipping_driver_command()
def confirm_otg_move_task_unit_quantity(
    *,
    legacy_order_id: str,
    unit_quantity,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    with transaction.atomic():
        _adopt_active_otg_task(
            legacy_order_id,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        return shared_execution.confirm_move_task_unit_quantity(
            legacy_order_id=legacy_order_id,
            unit_quantity=unit_quantity,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )


def _apply_unit_shortage_to_payload(
    payload: dict,
    placement_payload: dict,
    *,
    pallet_code: str,
    box_code: str,
    barcode: str,
    expected_qty: int,
    actual_qty: int,
) -> dict:
    """Reduce only this task's runtime plan after the warehouse correction."""
    adjusted = copy.deepcopy(payload)
    cached_placement = copy.deepcopy(placement_payload)
    missing_qty = expected_qty - actual_qty
    consumed, consume_error, consumed_qty = shared_execution._consume_box_qty(
        cached_placement,
        pallet_code,
        box_code,
        missing_qty,
        {barcode},
        set(),
        set(),
    )
    if not consumed or consumed_qty != missing_qty:
        raise ValueError(
            consume_error
            or "Не удалось сверить недостачу с составом короба. Складские данные не изменены."
        )

    adjusted.setdefault("unit_shortage_original_requested_qty", expected_qty)
    adjusted["requested_qty"] = actual_qty
    adjusted["available_qty"] = actual_qty
    adjusted["mobile_placement_payload"] = cached_placement

    requested_barcode_qty = shared_execution._normalize_barcode_qty_map(
        adjusted.get("requested_barcode_qty")
    )
    if requested_barcode_qty:
        requested_barcode_qty[barcode] = actual_qty
        adjusted["requested_barcode_qty"] = requested_barcode_qty

    requested_rows = []
    for raw_row in adjusted.get("requested_rows") or []:
        if not isinstance(raw_row, dict):
            continue
        row = copy.deepcopy(raw_row)
        row_box = str(row.get("box_code") or "").strip()
        row_barcodes = shared_execution._normalize_barcode_qty_map(row.get("barcode_qty"))
        if row_box.casefold() == box_code.casefold() and barcode in row_barcodes:
            row_barcodes[barcode] = actual_qty
            row["barcode_qty"] = row_barcodes
            row["qty"] = actual_qty
        requested_rows.append(row)
    if requested_rows:
        adjusted["requested_rows"] = requested_rows

    partial_patterns = []
    for raw_pattern in adjusted.get("partial_pick_patterns") or []:
        if not isinstance(raw_pattern, dict):
            continue
        pattern = copy.deepcopy(raw_pattern)
        pick_barcodes = shared_execution._normalize_barcode_qty_map(
            pattern.get("barcode_qty") or pattern.get("pick_barcode_qty")
        )
        source_barcodes = shared_execution._normalize_barcode_qty_map(
            pattern.get("source_barcode_qty")
        )
        if barcode in pick_barcodes:
            pick_barcodes[barcode] = actual_qty
            pattern["barcode_qty"] = pick_barcodes
            pattern["pick_qty"] = actual_qty
        if barcode in source_barcodes:
            source_barcodes[barcode] = actual_qty
            pattern["source_barcode_qty"] = source_barcodes
            pattern["source_box_qty"] = max(
                shared_execution._as_int(pattern.get("source_box_qty")) - missing_qty,
                actual_qty,
            )
        partial_patterns.append(pattern)
    if partial_patterns:
        adjusted["partial_pick_patterns"] = partial_patterns

    box_patterns = []
    for raw_pattern in adjusted.get("requested_box_patterns") or []:
        if not isinstance(raw_pattern, dict):
            continue
        pattern = copy.deepcopy(raw_pattern)
        pattern_barcodes = shared_execution._normalize_barcode_qty_map(
            pattern.get("barcode_qty")
        )
        if barcode in pattern_barcodes:
            pattern_barcodes[barcode] = actual_qty
            pattern["barcode_qty"] = pattern_barcodes
            pattern["box_qty"] = max(
                shared_execution._as_int(pattern.get("box_qty")) - missing_qty,
                actual_qty,
            )
        box_patterns.append(pattern)
    if box_patterns:
        adjusted["requested_box_patterns"] = box_patterns

    request_items = []
    remaining_request_reduction = missing_qty
    for raw_item in adjusted.get("request_items") or []:
        if not isinstance(raw_item, dict):
            continue
        item = copy.deepcopy(raw_item)
        item_qty = shared_execution._as_int(item.get("requested_qty"))
        if item_qty > 0 and remaining_request_reduction > 0:
            reduction = min(item_qty, remaining_request_reduction)
            item["requested_qty"] = item_qty - reduction
            remaining_request_reduction -= reduction
        request_items.append(item)
    if request_items:
        adjusted["request_items"] = request_items

    route_plan = dict(adjusted.get("route_plan") or {})
    if route_plan:
        route_plan["qty_to_pick"] = actual_qty
        adjusted["route_plan"] = route_plan

    adjusted["unit_shortage_runtime_plan"] = {
        "box_code": box_code,
        "barcode": barcode,
        "expected_qty": expected_qty,
        "actual_qty": actual_qty,
        "missing_qty": missing_qty,
        "adjusted_at": timezone.localtime().isoformat(),
    }
    return adjusted


def _unit_shortage_verification_task(
    *,
    task: MoveTask,
    payload: dict,
    box_code: str,
    barcode: str,
    expected_qty: int,
    actual_qty: int,
    operation_id: int,
    user,
    employee_name: str,
):
    from employees.models import Employee
    from todo.models import Task

    head_manager = (
        Employee.objects.select_for_update()
        .filter(role="head_manager", is_active=True)
        .order_by("full_name", "id")
        .first()
    )
    if head_manager is None:
        raise ValueError("Не найден активный начальник склада для проверки недостачи.")
    marker = f"[otg-unit-shortage:{task.id}:{box_code.casefold()}:{barcode}]"
    existing = (
        Task.objects.filter(description__contains=marker)
        .exclude(status="done")
        .order_by("-created_at", "-id")
        .first()
    )
    if existing is not None:
        return existing

    missing_qty = expected_qty - actual_qty
    pallet_code = str(payload.get("pallet_code") or task.pallet_code or "").strip()
    source_label = str(payload.get("from_label") or "").strip()
    shipping_order_id = str(payload.get("shipping_order_id") or "").strip()
    description = "\n".join(
        [
            f"Заявка OTG: {shipping_order_id or '-'}",
            f"Задание ричтрака: {task.legacy_order_id}",
            f"Водитель: {employee_name or '-'}",
            f"Паллета: {pallet_code or '-'}",
            f"Короб: {box_code}",
            f"Место: {source_label or '-'}",
            f"Штрихкод: {barcode}",
            f"Ожидалось: {expected_qty} шт.; найдено: {actual_qty} шт.; недостача: {missing_qty} шт.",
            (
                "Остаток атомарно скорректирован. Проверьте расхождение и "
                "организуйте отдельный добор недостающего товара."
            ),
            f"Складская операция проверки: {operation_id}",
            "",
            marker,
        ]
    )
    return Task.objects.create(
        title="СРОЧНО: штучная недостача при отборе OTG",
        description=description,
        route="/head-manager/stock-editor/",
        assigned_to=head_manager,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        status="in_progress",
        priority="urgent",
        due_date=timezone.localtime(),
    )


@transaction.atomic
@shipping_driver_command()
def report_otg_unit_shortage(
    *,
    legacy_order_id: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    task = _load_live_otg_task(legacy_order_id, for_update=True)
    if task is None:
        return MoveTaskCommandResult(ok=False, error="Живое задание OTG не найдено.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="Профиль сотрудника не найден.")

    payload = dict(task.payload or {})
    if not shared_execution._uses_otg_scan_fact_mode(payload):
        return MoveTaskCommandResult(ok=False, error="Для этого задания штучная недостача недоступна.")
    if _otg_task_status(task, payload) != MoveTask.STATUS_IN_PROGRESS:
        return MoveTaskCommandResult(ok=False, error="Сначала возьмите задание в работу.")
    assigned_to_id = _otg_task_assignee_employee_id(task, payload)
    if assigned_to_id and assigned_to_id != employee_id:
        return MoveTaskCommandResult(ok=False, error="Задание назначено другому водителю.")
    previous_reports = [
        dict(report)
        for report in (payload.get("unit_shortage_reports") or [])
        if isinstance(report, dict)
    ]
    payload, placement_payload, _placement_source, payload_changed, error = (
        shared_execution._ensure_mobile_runtime_payload(task, payload)
    )
    if error:
        return MoveTaskCommandResult(ok=False, error=error)
    if payload_changed:
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
    snapshot = shared_execution._build_mobile_execution_snapshot_payload(
        task,
        payload,
        placement_payload,
    )
    enriched = _enrich_otg_box_snapshot(task, snapshot)
    entry = dict(enriched.get("unit_shortage_entry") or {})
    if enriched.get("current_step") != "units" or not entry.get("available"):
        if previous_reports:
            return MoveTaskCommandResult(
                ok=True,
                task=task,
                payload=payload,
                message="Эта недостача уже зафиксирована. Продолжайте доставку в OTG.",
            )
        return MoveTaskCommandResult(
            ok=False,
            error=(
                "Недостачу можно зафиксировать только после скана фактически найденных "
                "единиц в полностью опустошённом коробе."
            ),
        )

    box_code = str(entry.get("box_code") or "").strip()
    barcode = str(entry.get("barcode") or "").strip()
    expected_qty = shared_execution._as_int(entry.get("required_qty"))
    actual_qty = shared_execution._as_int(entry.get("current_qty"))
    missing_qty = expected_qty - actual_qty
    matching_report = next(
        (
            report
            for report in previous_reports
            if str(report.get("box_code") or "").strip().casefold()
            == box_code.casefold()
            and str(report.get("barcode") or "").strip() == barcode
        ),
        None,
    )
    if matching_report is not None:
        return MoveTaskCommandResult(
            ok=True,
            task=task,
            payload=payload,
            message="Эта недостача уже зафиксирована. Продолжайте доставку в OTG.",
        )
    if expected_qty <= actual_qty or actual_qty <= 0 or missing_qty <= 0:
        return MoveTaskCommandResult(ok=False, error="Некорректное количество штучной недостачи.")

    from sklad.services.warehouse_write_path import WarehouseWritePathService

    try:
        shortage_result = WarehouseWritePathService.report_otg_unit_shortage(
            agency=task.request.agency,
            order_id=str(payload.get("shipping_order_id") or "").strip(),
            move_task_id=str(task.id),
            source_pallet_code=str(payload.get("pallet_code") or task.pallet_code or "").strip(),
            source_box_code=box_code,
            barcode=barcode,
            expected_qty=expected_qty,
            actual_qty=actual_qty,
            performed_by=user if getattr(user, "is_authenticated", False) else None,
        )
        verification_task = _unit_shortage_verification_task(
            task=task,
            payload=payload,
            box_code=box_code,
            barcode=barcode,
            expected_qty=expected_qty,
            actual_qty=actual_qty,
            operation_id=shortage_result.operation_id,
            user=user,
            employee_name=employee_name,
        )
        payload = _apply_unit_shortage_to_payload(
            payload,
            placement_payload,
            pallet_code=str(payload.get("pallet_code") or task.pallet_code or "").strip(),
            box_code=box_code,
            barcode=barcode,
            expected_qty=expected_qty,
            actual_qty=actual_qty,
        )
    except ValueError as exc:
        transaction.set_rollback(True)
        return MoveTaskCommandResult(ok=False, error=str(exc))

    report = {
        "box_code": box_code,
        "barcode": barcode,
        "expected_qty": expected_qty,
        "actual_qty": actual_qty,
        "missing_qty": missing_qty,
        "reported_at": timezone.localtime().isoformat(),
        "reported_by": str(employee_name or "").strip(),
        "warehouse_event_ids": list(shortage_result.event_ids),
        "warehouse_operation_id": shortage_result.operation_id,
        "verification_task_id": int(verification_task.id),
        "released_reserve_qty": shortage_result.released_reserve_qty,
    }
    reports = [
        dict(item)
        for item in (payload.get("unit_shortage_reports") or [])
        if isinstance(item, dict)
    ]
    reports.append(report)
    payload["unit_shortage_reports"] = reports
    task.payload = payload
    task.save(update_fields=["payload", "updated_at"])
    return MoveTaskCommandResult(
        ok=True,
        task=task,
        payload=payload,
        message=(
            f"Недостача {missing_qty} шт. зафиксирована. В OTG поедет {actual_qty} шт.; "
            "недостающее количество отправлено на проверку и отдельный добор."
        ),
    )


@shipping_driver_command()
def report_otg_no_stock(
    *,
    legacy_order_id: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    with transaction.atomic():
        task = _load_live_otg_task(legacy_order_id, for_update=True)
        if not task:
            return MoveTaskCommandResult(ok=False, error="Живое задание OTG не найдено.")
        payload = dict(task.payload or {})
        if not shared_execution._uses_otg_scan_fact_mode(payload):
            return MoveTaskCommandResult(ok=False, error="Для этого задания недоступна фиксация «Нет на месте».")
        if _otg_task_status(task, payload) != MoveTask.STATUS_IN_PROGRESS:
            return MoveTaskCommandResult(ok=False, error="Сначала возьмите задание в работу.")
        assigned_to_id = _otg_task_assignee_employee_id(task, payload)
        if assigned_to_id and assigned_to_id != employee_id:
            return MoveTaskCommandResult(ok=False, error="Задание назначено другому водителю.")
        execution = dict(payload.get("mobile_execution") or {})
        scanned_box_codes: list[str] = []
        scanned_box_keys: set[str] = set()
        for raw_code in execution.get("boxes_scanned") or []:
            code = str(raw_code or "").strip()
            key = code.casefold()
            if not code or key in scanned_box_keys:
                continue
            scanned_box_keys.add(key)
            scanned_box_codes.append(code)

        active_claim_codes = {
            str(code or "").strip().casefold()
            for code in BoxClaim.objects.filter(
                move_task=task,
                status=BoxClaim.STATUS_CLAIMED,
            ).values_list("box_code", flat=True)
            if str(code or "").strip()
        }
        if scanned_box_codes:
            if execution.get("units_scanned"):
                return MoveTaskCommandResult(
                    ok=False,
                    error="Штучную недостачу зафиксируйте кнопкой количества товара.",
                )
            if active_claim_codes - scanned_box_keys:
                return MoveTaskCommandResult(
                    ok=False,
                    error="По заданию есть другой подтверждённый подбор. Нужен разбор задания.",
                )

            current_snapshot = shared_execution.build_mobile_execution_snapshot(task.legacy_order_id)
            requested_box_count = max(
                shared_execution._as_int(current_snapshot.get("boxes_total")),
                shared_execution._as_int(payload.get("requested_box_count")),
            )
            scanned_box_count = len(scanned_box_codes)
            if (
                str(current_snapshot.get("current_step") or "").strip() != "boxes"
                or requested_box_count <= 0
                or scanned_box_count >= requested_box_count
            ):
                return MoveTaskCommandResult(
                    ok=False,
                    error="В этом шаге отсутствующий остаток уже нельзя фиксировать.",
                )

            payload, runtime_error = _otg_current_source_runtime_payload(task, payload)
            if runtime_error:
                return MoveTaskCommandResult(ok=False, error=runtime_error)
            placement_payload = shared_execution._cached_mobile_placement_payload(payload)
            pallet_code = str(payload.get("pallet_code") or task.pallet_code or "").strip()
            requested_patterns = [
                dict(pattern)
                for pattern in (payload.get("requested_box_patterns") or [])
                if isinstance(pattern, dict)
            ]
            if requested_patterns:
                selected_codes, adjusted_patterns = (
                    shared_execution._assign_partial_requested_patterns_to_boxes(
                        payload,
                        placement_payload,
                        pallet_code,
                        scanned_box_codes,
                    )
                )
                if len(selected_codes) != scanned_box_count or not adjusted_patterns:
                    return MoveTaskCommandResult(
                        ok=False,
                        error="Не удалось однозначно определить отсутствующий остаток коробов.",
                    )
                scanned_box_codes = list(selected_codes)
                scanned_box_keys = {code.casefold() for code in scanned_box_codes}
                payload["requested_box_patterns"] = adjusted_patterns

            box_qty_by_code = {
                str(row.get("box_code") or "").strip().casefold(): max(
                    shared_execution._as_int(row.get("box_qty")),
                    0,
                )
                for row in (current_snapshot.get("boxes") or [])
                if isinstance(row, dict) and str(row.get("box_code") or "").strip()
            }
            actual_qty = sum(box_qty_by_code.get(code.casefold(), 0) for code in scanned_box_codes)
            original_requested_qty = max(
                shared_execution._as_int(payload.get("requested_qty")),
                shared_execution._as_int(task.qty_planned),
                sum(box_qty_by_code.values()),
                0,
            )
            if actual_qty <= 0 and original_requested_qty > 0:
                actual_qty = max(
                    (original_requested_qty * scanned_box_count) // requested_box_count,
                    scanned_box_count,
                )
            if actual_qty <= 0:
                actual_qty = scanned_box_count

            missing_box_count = requested_box_count - scanned_box_count
            pending_codes = [
                str(row.get("box_code") or "").strip()
                for row in (current_snapshot.get("boxes_pending") or [])
                if isinstance(row, dict)
                and str(row.get("box_code") or "").strip()
                and str(row.get("box_code") or "").strip().casefold() not in scanned_box_keys
            ]
            original_reserved_codes = [
                str(code or "").strip()
                for code in (payload.get("reserved_box_codes") or [])
                if str(code or "").strip()
            ]
            remaining_reserved_codes = [
                code for code in original_reserved_codes if code.casefold() not in scanned_box_keys
            ]

            payload["requested_box_count"] = scanned_box_count
            payload["requested_qty"] = actual_qty
            payload["available_qty"] = actual_qty
            payload["picked_boxes"] = list(scanned_box_codes)
            payload["picked_qty"] = actual_qty
            # The driver has now established the exact physical fact. Pin the
            # current task to those scanned boxes even when the original task
            # used late/pattern matching; otherwise the mobile snapshot keeps
            # showing every matching box on the pallet and never advances.
            payload["requested_box_selection"] = shared_execution.BOX_SELECTION_FIXED
            payload["move_mode"] = MoveTask.MODE_BOX_FULL
            payload["requested_boxes"] = list(scanned_box_codes)
            payload["requested_box"] = scanned_box_codes[0] if len(scanned_box_codes) == 1 else ""
            payload["late_box_choice"] = False
            payload["candidate_box_codes"] = list(scanned_box_codes)
            payload["reserved_box_codes"] = list(scanned_box_codes)
            payload["planned_box_codes"] = list(scanned_box_codes)
            payload["selected_box_codes"] = list(scanned_box_codes)
            payload["otg_boxes_planned"] = scanned_box_count
            route_plan = dict(payload.get("route_plan") or {})
            route_plan["boxes_to_pick"] = scanned_box_count
            route_plan["qty_to_pick"] = actual_qty
            payload["route_plan"] = route_plan
            payload["flexible_pallet_adjustment"] = {
                "pallet_code": pallet_code,
                "original_requested_box_count": requested_box_count,
                "selected_requested_box_count": scanned_box_count,
                "remaining_box_count": missing_box_count,
                "original_requested_qty": original_requested_qty,
                "selected_requested_qty": actual_qty,
                "remaining_requested_qty": max(original_requested_qty - actual_qty, 0),
                "selected_actual_box_codes": list(scanned_box_codes),
                "remaining_reserved_box_codes": remaining_reserved_codes,
                "reported_missing_box_codes": pending_codes[:missing_box_count],
                "reason": "driver_reported_remaining_boxes_missing",
            }
            report = {
                "pallet_code": pallet_code,
                "from_location": dict(payload.get("from_location") or {}),
                "reported_at": timezone.localtime().isoformat(),
                "reported_by": str(employee_name or "").strip(),
                "partial": True,
                "found_box_codes": list(scanned_box_codes),
                "missing_box_codes": pending_codes[:missing_box_count],
                "found_box_count": scanned_box_count,
                "missing_box_count": missing_box_count,
            }
            reports = list(payload.get("no_stock_reports") or [])
            reports.append(report)
            payload["no_stock_reports"] = reports
            task.move_mode = MoveTask.MODE_BOX_FULL
            task.qty_planned = actual_qty
            task.payload = payload
            task.save(update_fields=["move_mode", "qty_planned", "payload", "updated_at"])
            return MoveTaskCommandResult(
                ok=True,
                task=task,
                payload=payload,
                message=(
                    f"Отсутствующий остаток зафиксирован: {missing_box_count} кор. "
                    f"Отвезите найденные {scanned_box_count} кор. в OTG; на остаток будет создан добор."
                ),
            )

        if active_claim_codes:
            return MoveTaskCommandResult(
                ok=False,
                error="По заданию уже есть подтверждённый факт подбора. Нужен разбор задания.",
            )

        report = {
            "pallet_code": str(payload.get("pallet_code") or task.pallet_code or "").strip(),
            "from_location": dict(payload.get("from_location") or {}),
            "reported_at": timezone.localtime().isoformat(),
            "reported_by": str(employee_name or "").strip(),
        }
        reports = list(payload.get("no_stock_reports") or [])
        reports.append(report)
        payload["no_stock_reports"] = reports
        payload["status"] = MoveTask.STATUS_FAILED
        payload["status_label"] = "Нет на месте"
        payload["blocked_reason"] = "no_stock_at_planned_pallet"
        requested_box_count = max(
            shared_execution._as_int(payload.get("requested_box_count")),
            len(_otg_payload_selected_box_codes(payload)),
            1,
        )
        requested_qty = max(
            shared_execution._as_int(payload.get("requested_qty")),
            int(task.qty_planned or 0),
            requested_box_count,
        )
        missing_box_codes = _otg_payload_selected_box_codes(payload)
        payload["flexible_pallet_adjustment"] = {
            "pallet_code": report["pallet_code"],
            "original_requested_box_count": requested_box_count,
            "selected_requested_box_count": 0,
            "remaining_box_count": requested_box_count,
            "original_requested_qty": requested_qty,
            "selected_requested_qty": 0,
            "remaining_requested_qty": requested_qty,
            "selected_actual_box_codes": [],
            "remaining_reserved_box_codes": list(missing_box_codes),
            "reported_missing_box_codes": list(missing_box_codes),
            "reason": "driver_reported_remaining_boxes_missing",
        }
        task.status = MoveTask.STATUS_FAILED
        task.payload = payload
        task.save(update_fields=["status", "payload", "updated_at"])
        sync_task_status_by_legacy_order_id(
            task.legacy_order_id,
            status=MoveTask.STATUS_FAILED,
            assigned_to=user if getattr(user, "is_authenticated", False) else None,
            assigned_to_name=employee_name,
        )
        shared_execution._maybe_replan_adjusted_otg_remainder(
            task=task,
            payload=payload,
            user=user if getattr(user, "is_authenticated", False) else None,
            employee_name=employee_name,
        )
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        created_move_ids = list(
            (payload.get("otg_runtime_replan") or {}).get("created_move_ids") or []
        )
        if created_move_ids:
            message = (
                "Нет на месте зафиксировано. Система исключила отсутствующую паллету "
                "и автоматически создала новое задание с другого места."
            )
        else:
            message = (
                "Нет на месте зафиксировано. Подходящей замены сейчас нет; "
                "кладовщику показана недостача для разбора."
            )
        return MoveTaskCommandResult(
            ok=True,
            task=task,
            payload=payload,
            message=message,
        )


@shipping_driver_command()
def complete_otg_move_task(
    *,
    legacy_order_id: str,
    user,
    employee_id: int | None,
    employee_name: str,
    require_scan_confirmation: bool = True,
    desktop_selected_boxes: list[str] | None = None,
) -> MoveTaskCommandResult:
    with transaction.atomic():
        _adopt_active_otg_task(
            legacy_order_id,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        return shared_execution.complete_move_task(
            legacy_order_id=legacy_order_id,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
            require_scan_confirmation=require_scan_confirmation,
            desktop_selected_boxes=desktop_selected_boxes,
        )


def _prepare_active_request_box_scan(
    legacy_order_ids,
    *,
    scan_value: str,
    user,
    employee_id: int | None,
):
    if not employee_id:
        return OtgReserveSwapResult()
    visible_order_ids = _otg_request_visible_legacy_order_ids(legacy_order_ids)
    if not visible_order_ids:
        return OtgReserveSwapResult()
    snapshot = build_otg_mobile_request_execution_snapshot(
        visible_order_ids,
        employee_id=employee_id,
    )
    active_order_id = str((snapshot or {}).get("active_order_id") or "").strip()
    active_step = str((snapshot or {}).get("current_step") or "").strip()
    if not active_order_id or active_step != "boxes":
        return OtgReserveSwapResult()
    if bool((snapshot or {}).get("destination_override_pending")):
        return OtgReserveSwapResult()
    return prepare_otg_box_scan(
        legacy_order_id=active_order_id,
        scan_value=scan_value,
        user=user,
    )
