from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from employees.models import Employee
from fbs.exceptions import FbsError
from reachtruck.models import BoxClaim, MoveTask, PalletLock
from sklad.models import WarehouseContainer, WarehouseEvent
from todo.models import Task

@dataclass(frozen=True)
class MissingBoxReportResult:
    ok: bool
    message: str = ""
    error: str = ""
    created: bool = False
    replacement_box_code: str = ""
    replacement_pallet_code: str = ""
    replacement_source_code: str = ""
    partial_completed: bool = False


def _clean(value) -> str:
    return str(value or "").strip()


def _pending_box_codes(snapshot: dict) -> list[str]:
    result: list[str] = []
    for row in list(snapshot.get("boxes_pending") or []):
        code = _clean(row.get("box_code") if isinstance(row, dict) else row)
        if code and code.casefold() not in {item.casefold() for item in result}:
            result.append(code)
    return result


def _payload_box_codes(payload: dict) -> set[str]:
    values: set[str] = set()
    for field_name in (
        "requested_boxes",
        "planned_box_codes",
        "selected_box_codes",
        "reserved_box_codes",
        "candidate_box_codes",
    ):
        raw_values = payload.get(field_name) or []
        if isinstance(raw_values, str):
            raw_values = raw_values.split(",")
        values.update(_clean(value).casefold() for value in raw_values if _clean(value))
    for field_name in ("requested_box", "picked_box"):
        value = _clean(payload.get(field_name))
        if value:
            values.add(value.casefold())
    return values


def _blocked_box_codes_for_replacement(task: MoveTask, missing_box_code: str) -> set[str]:
    blocked: set[str] = set()
    open_tasks = (
        MoveTask.objects.select_related("request")
        .filter(status__in=(MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS))
        .exclude(pk=task.pk)
    )
    if task.request_id and task.request.agency_id:
        open_tasks = open_tasks.filter(request__agency_id=task.request.agency_id)
    for other_task in open_tasks.only("payload", "request_id"):
        blocked.update(_payload_box_codes(dict(other_task.payload or {})))
    blocked.update(
        _payload_box_codes(dict(task.payload or {})) - {_clean(missing_box_code).casefold()}
    )
    return blocked


def _replace_code_in_payload(payload: dict, old_code: str, new_code: str) -> None:
    old_key = _clean(old_code).casefold()
    for field_name in (
        "requested_boxes",
        "planned_box_codes",
        "selected_box_codes",
        "reserved_box_codes",
        "candidate_box_codes",
    ):
        raw_values = payload.get(field_name)
        if not isinstance(raw_values, list):
            continue
        payload[field_name] = [
            new_code if _clean(value).casefold() == old_key else value
            for value in raw_values
        ]
    if _clean(payload.get("requested_box")).casefold() == old_key:
        payload["requested_box"] = new_code


def _location_payload(location) -> dict:
    return {
        "zone": _clean(getattr(location, "zone_code", "")),
        "row": int(getattr(location, "row_no", 0) or 0),
        "section": int(getattr(location, "section_no", 0) or 0),
        "tier": int(getattr(location, "tier_no", 0) or 0),
        "cell": int(getattr(location, "cell_no", 0) or 0),
    }


@transaction.atomic
def _replace_missing_main_warehouse_box(
    *,
    task: MoveTask,
    missing_box_code: str,
    snapshot: dict,
    user,
) -> dict:
    """Retarget an active non-shipping move without changing stock quantities."""
    task = MoveTask.objects.select_for_update().select_related("request").get(pk=task.pk)
    payload = dict(task.payload or {})
    to_zone = _clean((payload.get("to_location") or {}).get("zone") or task.to_zone).upper()
    if (
        to_zone == "OTG"
        or to_zone == "OBR"
        or payload.get("shipping_order_id")
        or payload.get("otg_delivery_request_id")
        or payload.get("processing_order_id")
        or payload.get("fbs_replenishment_bridge_v1")
    ):
        return {"replaced": False, "supported": False}

    agency_id = int(task.request.agency_id or 0)
    missing_container_id = (
        WarehouseContainer.objects
        .filter(
            agency_id=agency_id,
            container_type=WarehouseContainer.TYPE_BOX,
        )
        .filter(
            Q(container_code__iexact=_clean(missing_box_code))
            | Q(snapshots__container_code__iexact=_clean(missing_box_code))
        )
        .distinct()
        .order_by("id")
        .values_list("id", flat=True)
        .first()
    )
    missing_container = (
        WarehouseContainer.objects.select_for_update().filter(pk=missing_container_id).first()
        if missing_container_id
        else None
    )
    if missing_container is None:
        return {"replaced": False, "supported": True}

    from sklad.services.missing_box_replacement import (
        find_exact_free_box_replacement,
        quarantine_missing_box,
    )

    blocked_codes = _blocked_box_codes_for_replacement(task, missing_box_code)
    replacement = find_exact_free_box_replacement(
        agency_id=agency_id,
        missing_container_id=missing_container.id,
        parent_container_id=missing_container.parent_container_id,
        blocked_box_codes=blocked_codes,
    )
    execution = dict(payload.get("mobile_execution") or {})
    boxes_scanned = [
        _clean(code) for code in execution.get("boxes_scanned") or [] if _clean(code)
    ]
    all_box_codes = {
        _clean(row.get("box_code") if isinstance(row, dict) else row).casefold()
        for row in list(snapshot.get("boxes") or [])
        if _clean(row.get("box_code") if isinstance(row, dict) else row)
    }
    allow_other_pallet = not boxes_scanned and len(all_box_codes) == 1
    if replacement is None and allow_other_pallet:
        replacement = find_exact_free_box_replacement(
            agency_id=agency_id,
            missing_container_id=missing_container.id,
            blocked_box_codes=blocked_codes,
        )

    if replacement is not None:
        same_pallet = missing_container.parent_container_id == replacement.pallet.id
        if not same_pallet and not allow_other_pallet:
            replacement = None

    if replacement is None:
        quarantine_operation = quarantine_missing_box(
            container_id=missing_container.id,
            agency_id=agency_id,
            context_type="reachtruck_move",
            context_id=str(task.id),
            performed_by=user,
        )
        return {"replaced": False, "supported": True, "partial_completed": False}

    replacement_code = _clean(replacement.container.container_code)
    replacement_pallet_code = _clean(replacement.pallet.container_code)
    replacement_location = replacement.location
    same_pallet = missing_container.parent_container_id == replacement.pallet.id

    if not same_pallet:
        # A pallet change is safe only before any box/unit scan and for one box.
        now = timezone.now()
        BoxClaim.objects.filter(
            move_task=task,
            status=BoxClaim.STATUS_CLAIMED,
        ).update(status=BoxClaim.STATUS_CANCELLED, released_at=now, updated_at=now)
        PalletLock.objects.filter(
            move_task=task,
            status=PalletLock.STATUS_ACTIVE,
        ).update(status=PalletLock.STATUS_CANCELLED, released_at=now, updated_at=now)

    quarantine_operation = quarantine_missing_box(
        container_id=missing_container.id,
        agency_id=agency_id,
        context_type="reachtruck_move",
        context_id=str(task.id),
        performed_by=user,
    )
    BoxClaim.objects.filter(
        move_task=task,
        box_code__iexact=_clean(missing_box_code),
        status=BoxClaim.STATUS_CLAIMED,
    ).update(
        status=BoxClaim.STATUS_CANCELLED,
        released_at=timezone.now(),
        updated_at=timezone.now(),
    )

    _replace_code_in_payload(payload, missing_box_code, replacement_code)
    location_data = _location_payload(replacement_location)
    payload["pallet_code"] = replacement_pallet_code
    payload["from_location"] = location_data
    payload["from_label"] = _clean(
        getattr(replacement_location, "display_name", "")
        or getattr(replacement_location, "location_code", "")
    )
    payload["from_code"] = _clean(getattr(replacement_location, "location_code", ""))
    payload["source_code"] = payload["from_code"]
    execution.update(
        {
            "source_confirmed": False,
            "pallet_confirmed": False,
            "destination_confirmed": False,
            "destination_override_pending": False,
            "boxes_scanned": boxes_scanned if same_pallet else [],
            "units_scanned": execution.get("units_scanned") if same_pallet else {},
            "last_scan": "",
        }
    )
    payload["mobile_execution"] = execution
    payload.pop("mobile_placement_payload", None)
    payload.pop("mobile_placement_source", None)
    payload["missing_box_replacement"] = {
        "missing_box_code": _clean(missing_box_code),
        "replacement_box_code": replacement_code,
        "replacement_pallet_code": replacement_pallet_code,
        "replacement_source_code": payload["source_code"],
        "replaced_at": timezone.localtime().isoformat(),
    }
    task.pallet_code = replacement_pallet_code
    task.from_zone = location_data["zone"]
    task.from_row = location_data["row"] or None
    task.from_section = location_data["section"] or None
    task.from_tier = location_data["tier"] or None
    task.from_cell = location_data["cell"] or None
    task.payload = payload
    task.save(
        update_fields=[
            "pallet_code",
            "from_zone",
            "from_row",
            "from_section",
            "from_tier",
            "from_cell",
            "payload",
            "updated_at",
        ]
    )

    from .claims import claim_boxes_for_task

    claim_boxes_for_task(
        task,
        [replacement_code],
        claimed_by=user,
        payload={"missing_box_replacement": True},
    )
    WarehouseEvent.objects.create(
        agency_id=agency_id,
        event_type="reachtruck_missing_box_replaced",
        stock_context_type="reachtruck_move",
        stock_context_id=str(task.id),
        container=replacement.container,
        operation=quarantine_operation,
        source_document_type="reachtruck_move",
        source_document_id=str(task.id),
        from_location=replacement_location,
        from_zone_code=_clean(getattr(replacement_location, "zone_code", "")),
        qty=sum(int(row.qty or 0) for row in replacement.snapshots),
        payload={
            "missing_box_code": _clean(missing_box_code),
            "replacement_box_code": replacement_code,
            "accounting_qty_unchanged": True,
        },
        performed_by=user if getattr(user, "is_authenticated", False) else None,
        performed_by_role="reachtruck_driver",
        occurred_at=timezone.now(),
    )
    return {
        "replaced": True,
        "supported": True,
        "box_code": replacement_code,
        "pallet_code": replacement_pallet_code,
        "source_code": payload["source_code"],
    }


def _head_manager_employee() -> Employee | None:
    return (
        Employee.objects.select_for_update()
        .filter(role="head_manager", is_active=True)
        .order_by("full_name", "id")
        .first()
    )


def create_missing_box_verification_task(
    *,
    box_code: str,
    source_kind: str,
    source_id: str,
    description: str,
    route: str,
    user,
) -> tuple[Task | None, bool, str]:
    """Create one open verification task without touching warehouse state."""
    normalized_code = _clean(box_code)
    if not normalized_code:
        return None, False, "Код короба не указан."
    head_manager = _head_manager_employee()
    if head_manager is None:
        return None, False, "Не найден активный начальник склада для проверки."

    marker = f"[missing-box:{_clean(source_kind)}:{_clean(source_id)}:{normalized_code.casefold()}]"
    existing = (
        Task.objects.filter(
            title="СРОЧНО: короб не найден на месте",
            description__contains=marker,
        )
        .exclude(status="done")
        .order_by("-created_at", "-id")
        .first()
    )
    if existing is not None:
        return existing, False, "Информация уже отправлена на проверку. Задание осталось в работе."

    task = Task.objects.create(
        title="СРОЧНО: короб не найден на месте",
        description=f"{description.rstrip()}\n\n{marker}",
        route=_clean(route)[:255],
        assigned_to=head_manager,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        status="in_progress",
        priority="urgent",
        due_date=timezone.localtime(),
    )
    return task, True, "Информация отправлена на проверку. Задание осталось в работе."


@transaction.atomic
def report_move_task_missing_box(
    *,
    legacy_order_id: str,
    box_code: str,
    mobile_category: str,
    mobile_request_key: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MissingBoxReportResult:
    order_id = _clean(legacy_order_id)
    normalized_code = _clean(box_code)
    if _clean(mobile_category).lower() != "movement":
        return MissingBoxReportResult(
            ok=False,
            error="Замена отсутствующего короба доступна только в разделе перемещений.",
        )
    task = (
        MoveTask.objects.select_for_update(of=("self",))
        .select_related("request", "request__agency")
        .filter(legacy_order_id=order_id)
        .order_by("-updated_at", "-id")
        .first()
    )
    if task is None:
        return MissingBoxReportResult(ok=False, error="Задание перемещения не найдено.")
    if task.status != MoveTask.STATUS_IN_PROGRESS:
        return MissingBoxReportResult(ok=False, error="Сообщить можно только по заданию, взятому в работу.")
    if not employee_id:
        return MissingBoxReportResult(ok=False, error="Профиль сотрудника не найден.")

    payload = dict(task.payload or {})
    assigned_employee_ids = {
        int(value)
        for value in (payload.get("assigned_employee_id"), payload.get("assigned_to_id"))
        if str(value or "").strip().isdigit() and int(value) > 0
    }
    if assigned_employee_ids and int(employee_id) not in assigned_employee_ids:
        return MissingBoxReportResult(ok=False, error="Задание назначено другому водителю ричтрака.")
    if task.assigned_to_id and task.assigned_to_id != getattr(user, "id", None):
        return MissingBoxReportResult(ok=False, error="Задание назначено другому водителю ричтрака.")
    if _head_manager_employee() is None:
        return MissingBoxReportResult(
            ok=False,
            error="Не найден активный начальник склада для проверки.",
        )

    from .task_commands import build_mobile_execution_snapshot

    snapshot = build_mobile_execution_snapshot(order_id)
    if _clean(snapshot.get("current_step")) != "boxes":
        return MissingBoxReportResult(ok=False, error="Сейчас задание не находится на этапе поиска коробов.")
    pending_codes = _pending_box_codes(snapshot)
    expected_code = next(
        (code for code in pending_codes if code.casefold() == normalized_code.casefold()),
        "",
    )
    if not expected_code:
        return MissingBoxReportResult(ok=False, error="Этот короб уже найден или не относится к текущему заданию.")

    replacement_result = {"replaced": False, "supported": True}
    try:
        if payload.get("fbs_replenishment_bridge_v1"):
            from fbs.services.reachtruck_bridge import replace_missing_fbs_box_for_task

            replacement_result = replace_missing_fbs_box_for_task(
                task=task,
                missing_box_code=expected_code,
                user=user,
                employee_id=int(employee_id),
                employee_name=employee_name,
            )
            replacement_result.setdefault("supported", True)
        else:
            replacement_result = _replace_missing_main_warehouse_box(
                task=task,
                missing_box_code=expected_code,
                snapshot=snapshot,
                user=user,
            )
    except (FbsError, ValueError, RuntimeError) as exc:
        return MissingBoxReportResult(
            ok=False,
            error=_clean(exc) or "Не удалось безопасно исключить отсутствующий короб.",
        )

    replacement_box_code = _clean(replacement_result.get("box_code"))
    replacement_pallet_code = _clean(replacement_result.get("pallet_code"))
    replacement_source_code = _clean(replacement_result.get("source_code"))
    partial_completed = bool(replacement_result.get("partial_completed"))
    collected_count = int(replacement_result.get("collected_count") or 0)

    agency = getattr(task.request, "agency", None)
    agency_name = _clean(
        getattr(agency, "short_name", "")
        or getattr(agency, "agn_name", "")
        or getattr(agency, "name", "")
        or agency
    ) or "-"
    source_label = _clean(
        payload.get("source_location_scan_code")
        or payload.get("from_label")
        or snapshot.get("source_code")
        or snapshot.get("source_label")
    ) or "-"
    pallet_code = _clean(payload.get("pallet_code") or task.pallet_code) or "-"
    category = _clean(mobile_category) or _clean(payload.get("task_category")) or "movement"
    kind_label = _clean(payload.get("task_kind_label")) or "Перемещение на основном складе"
    route_params = {"mobile_category": category, "mobile_task": order_id}
    if _clean(mobile_request_key):
        route_params["mobile_request"] = _clean(mobile_request_key)
    route = f"/reachtruck/?{urlencode(route_params)}"
    replacement_description = ""
    if replacement_box_code:
        replacement_description = (
            "\n\nСистема атомарно назначила идентичный свободный короб:\n"
            f"Новый короб: {replacement_box_code}\n"
            f"Новая паллета: {replacement_pallet_code or '-'}\n"
            f"Новое место: {replacement_source_code or '-'}\n"
            "Учётное количество обоих коробов не списывалось. "
            "Отсутствующий короб заблокирован до проверки; рабочий резерв перенесён на замену."
        )
    elif partial_completed:
        replacement_description = (
            "\n\nОтсутствующий короб исключён из текущего перемещения. "
            f"Уже собрано коробов: {collected_count}. "
            "Подбор остальных коробов продолжается; отсутствующий короб заблокирован до проверки, "
            "его учётное количество не списывалось."
        )
    else:
        replacement_description = (
            "\n\nИдентичного свободного короба на складе не найдено. "
            "Отсутствующий короб заблокирован до проверки без списания учётного количества."
        )
    description = (
        "Водитель ричтрака сообщил, что ожидаемого короба нет на указанном месте.\n\n"
        f"Контур: {kind_label}\n"
        f"Задание: {order_id}\n"
        f"Водитель: {_clean(employee_name) or _clean(getattr(user, 'username', '')) or '-'}\n"
        f"Клиент: {agency_name}\n"
        f"Короб: {expected_code}\n"
        f"Паллета: {pallet_code}\n"
        f"Место по системе: {source_label}"
        f"{replacement_description}\n\n"
        "Нужно проверить фактическое наличие отсутствующего короба и складской учёт."
    )
    _task, created, message = create_missing_box_verification_task(
        box_code=expected_code,
        source_kind="move-task",
        source_id=str(task.id),
        description=description,
        route=route,
        user=user,
    )
    if _task is None:
        return MissingBoxReportResult(ok=False, error=message)
    if replacement_box_code:
        message = (
            f"Короб {expected_code} отправлен на проверку. "
            f"Назначен похожий короб {replacement_box_code}, "
            f"паллета {replacement_pallet_code or '-'}, место {replacement_source_code or '-'}. "
            "Отсканируйте новую паллету."
        )
    elif partial_completed:
        message = (
            f"Короб {expected_code} отмечен как отсутствующий и отправлен на проверку. "
            "Он исключён из текущего перемещения. Продолжайте подбор следующего короба."
        )
    elif replacement_result.get("supported", True):
        message = (
            f"Короб {expected_code} отправлен на проверку. "
            "Похожего свободного короба на складе нет; задание осталось в работе."
        )
    return MissingBoxReportResult(
        ok=True,
        message=message,
        created=created,
        replacement_box_code=replacement_box_code,
        replacement_pallet_code=replacement_pallet_code,
        replacement_source_code=replacement_source_code,
        partial_completed=partial_completed,
    )
