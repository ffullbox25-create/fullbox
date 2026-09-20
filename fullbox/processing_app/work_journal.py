from __future__ import annotations

import hashlib
from collections.abc import Iterable

from django.utils import timezone

from employees.models import Employee

from .models import ProcessingWorkEvent


def processing_work_event_key(operation_type: str, *parts) -> str:
    source = "\x1f".join(str(part or "").strip() for part in parts)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"{operation_type}:{digest}"


def _positive_int(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def record_processing_assignment_completion(
    *,
    order_id: str,
    agency,
    assignment: dict,
    occurred_at,
    recorded_by=None,
) -> tuple[ProcessingWorkEvent, bool]:
    assignment_id = str(assignment.get("id") or "").strip()
    if not assignment_id:
        assignment_id = processing_work_event_key(
            "assignment_fallback",
            order_id,
            assignment.get("card_id"),
            assignment.get("operation_key"),
            assignment.get("assignee_id"),
            occurred_at,
        )
    employee_id = _positive_int(assignment.get("assignee_id")) or None
    employee = (
        Employee.objects.filter(pk=employee_id).first()
        if employee_id
        else None
    )
    employee_name = str(
        getattr(employee, "full_name", "")
        or assignment.get("assignee_name")
        or ""
    ).strip()
    return ProcessingWorkEvent.objects.get_or_create(
        event_key=processing_work_event_key(
            ProcessingWorkEvent.TYPE_OPERATION_COMPLETED,
            order_id,
            assignment_id,
        ),
        defaults={
            "operation_type": ProcessingWorkEvent.TYPE_OPERATION_COMPLETED,
            "order_id": str(order_id),
            "agency": agency,
            "employee": employee,
            "employee_name": employee_name,
            "recorded_by": recorded_by if getattr(recorded_by, "is_authenticated", False) else None,
            "operation_key": str(assignment.get("operation_key") or "").strip()[:128],
            "operation_label": str(
                assignment.get("operation_label") or "Операция обработки"
            ).strip()[:255],
            "card_id": str(assignment.get("card_id") or "").strip()[:128],
            "units": _positive_int(assignment.get("actual_qty")),
            "boxes": 0,
            "occurred_at": occurred_at or timezone.now(),
            "metadata": {
                "assignment_id": assignment_id,
                "planned_qty": _positive_int(assignment.get("planned_qty")),
                "comment": str(assignment.get("comment") or "").strip(),
            },
        },
    )


def record_processing_box_completions(
    *,
    order_id: str,
    agency,
    boxes: Iterable[dict],
    occurred_at,
    recorded_by=None,
) -> int:
    box_rows = [box for box in boxes if isinstance(box, dict)]
    owner_user_ids = {
        _positive_int(box.get("owner_user_id"))
        for box in box_rows
        if _positive_int(box.get("owner_user_id"))
    }
    employees_by_user_id = {
        employee.user_id: employee
        for employee in Employee.objects.filter(user_id__in=owner_user_ids)
    }
    labels_to_employees: dict[str, list[Employee]] = {}
    owner_labels = {
        str(box.get("owner_user_label") or "").strip().casefold()
        for box in box_rows
        if str(box.get("owner_user_label") or "").strip()
    }
    if owner_labels:
        for employee in Employee.objects.filter(is_active=True):
            key = str(employee.full_name or "").strip().casefold()
            if key in owner_labels:
                labels_to_employees.setdefault(key, []).append(employee)

    pending = []
    for box in box_rows:
        box_code = str(box.get("code") or "").strip()
        if not box_code:
            continue
        owner_user_id = _positive_int(box.get("owner_user_id")) or None
        owner_label = str(box.get("owner_user_label") or "").strip()
        employee = employees_by_user_id.get(owner_user_id)
        if employee is None and owner_label:
            label_matches = labels_to_employees.get(owner_label.casefold(), [])
            if len(label_matches) == 1:
                employee = label_matches[0]
        units = sum(
            _positive_int(item.get("qty"))
            for item in (box.get("items") or [])
            if isinstance(item, dict)
        )
        pending.append(
            ProcessingWorkEvent(
                event_key=processing_work_event_key(
                    ProcessingWorkEvent.TYPE_BOX_FORMED,
                    order_id,
                    box_code,
                ),
                operation_type=ProcessingWorkEvent.TYPE_BOX_FORMED,
                order_id=str(order_id),
                agency=agency,
                employee=employee,
                employee_name=str(
                    getattr(employee, "full_name", "") or owner_label
                ).strip(),
                recorded_by=(
                    recorded_by
                    if getattr(recorded_by, "is_authenticated", False)
                    else None
                ),
                operation_key="box_formed",
                operation_label="Формирование короба",
                container_code=box_code[:128],
                units=units,
                boxes=1,
                occurred_at=occurred_at or timezone.now(),
                metadata={
                    "owner_user_id": owner_user_id,
                    "owner_user_label": owner_label,
                    "owner_agent_id": str(box.get("owner_agent_id") or "").strip(),
                    "direction": str(
                        box.get("direction") or box.get("direction_name") or ""
                    ).strip(),
                    "item_rows": sum(
                        1 for item in (box.get("items") or []) if isinstance(item, dict)
                    ),
                },
            )
        )
    if not pending:
        return 0
    before = set(
        ProcessingWorkEvent.objects.filter(
            event_key__in=[event.event_key for event in pending]
        ).values_list("event_key", flat=True)
    )
    ProcessingWorkEvent.objects.bulk_create(
        [event for event in pending if event.event_key not in before],
        ignore_conflicts=True,
        batch_size=500,
    )
    return len(pending) - len(before)
