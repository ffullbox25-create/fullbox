"""Сервисный слой переходов статусов «Других заявок».

Источник правды полей — OtherRequest. OrderAuditEntry остаётся журналом
и каналом совместимости с ЛК клиента / биллингом.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.models import log_order_action
from employees.models import Employee
from todo.models import Task

from .models import OtherRequest, OtherRequestCategory
from .other_requests import (
    ORDER_TYPE,
    close_open_tasks,
    create_manager_task,
    create_storekeeper_task,
    latest_other_entry,
    set_other_status,
)

# Новый статус → legacy-код в audit payload (для ЛК клиента).
NEW_TO_LEGACY_STATUS = {
    OtherRequest.STATUS_DRAFT: "draft",
    OtherRequest.STATUS_AWAITING_MANAGER: "submitted",
    OtherRequest.STATUS_NEED_CLIENT_INFO: "submitted",
    OtherRequest.STATUS_APPROVED: "submitted",
    OtherRequest.STATUS_AWAITING_DEPARTMENT: "warehouse",
    OtherRequest.STATUS_WAREHOUSE_ACCEPTED: "warehouse",
    OtherRequest.STATUS_IN_PROGRESS: "in_work",
    OtherRequest.STATUS_PAUSED: "in_work",
    OtherRequest.STATUS_DONE_BY_DEPARTMENT: "in_work",
    OtherRequest.STATUS_AWAITING_MANAGER_CHECK: "in_work",
    OtherRequest.STATUS_REWORK: "in_work",
    OtherRequest.STATUS_CLOSED: "completed",
    OtherRequest.STATUS_CANCELLED: "cancelled",
    OtherRequest.STATUS_REJECTED: "cancelled",
}

LEGACY_TO_NEW_STATUS = {
    "draft": OtherRequest.STATUS_DRAFT,
    "submitted": OtherRequest.STATUS_AWAITING_MANAGER,
    "warehouse": OtherRequest.STATUS_AWAITING_DEPARTMENT,
    "warehouse_accepted": OtherRequest.STATUS_WAREHOUSE_ACCEPTED,
    "in_work": OtherRequest.STATUS_IN_PROGRESS,
    "completed": OtherRequest.STATUS_CLOSED,
    "done": OtherRequest.STATUS_CLOSED,
    "cancelled": OtherRequest.STATUS_CANCELLED,
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    OtherRequest.STATUS_DRAFT: {
        OtherRequest.STATUS_AWAITING_MANAGER,
        OtherRequest.STATUS_CANCELLED,
    },
    OtherRequest.STATUS_AWAITING_MANAGER: {
        OtherRequest.STATUS_NEED_CLIENT_INFO,
        OtherRequest.STATUS_APPROVED,
        OtherRequest.STATUS_AWAITING_DEPARTMENT,
        OtherRequest.STATUS_REJECTED,
        OtherRequest.STATUS_CANCELLED,
    },
    OtherRequest.STATUS_NEED_CLIENT_INFO: {
        OtherRequest.STATUS_AWAITING_MANAGER,
        OtherRequest.STATUS_CANCELLED,
    },
    OtherRequest.STATUS_APPROVED: {
        OtherRequest.STATUS_AWAITING_DEPARTMENT,
        OtherRequest.STATUS_CANCELLED,
    },
    OtherRequest.STATUS_AWAITING_DEPARTMENT: {
        OtherRequest.STATUS_WAREHOUSE_ACCEPTED,
        OtherRequest.STATUS_AWAITING_MANAGER,
        OtherRequest.STATUS_CANCELLED,
    },
    OtherRequest.STATUS_WAREHOUSE_ACCEPTED: {
        OtherRequest.STATUS_IN_PROGRESS,
        OtherRequest.STATUS_PAUSED,
        OtherRequest.STATUS_CANCELLED,
    },
    OtherRequest.STATUS_IN_PROGRESS: {
        OtherRequest.STATUS_PAUSED,
        OtherRequest.STATUS_DONE_BY_DEPARTMENT,
        OtherRequest.STATUS_AWAITING_MANAGER_CHECK,
        OtherRequest.STATUS_NEED_CLIENT_INFO,
        OtherRequest.STATUS_CANCELLED,
    },
    OtherRequest.STATUS_PAUSED: {
        OtherRequest.STATUS_IN_PROGRESS,
        OtherRequest.STATUS_CANCELLED,
    },
    OtherRequest.STATUS_DONE_BY_DEPARTMENT: {
        OtherRequest.STATUS_AWAITING_MANAGER_CHECK,
        OtherRequest.STATUS_CANCELLED,
    },
    OtherRequest.STATUS_AWAITING_MANAGER_CHECK: {
        OtherRequest.STATUS_CLOSED,
        OtherRequest.STATUS_REWORK,
        OtherRequest.STATUS_NEED_CLIENT_INFO,
        OtherRequest.STATUS_CANCELLED,
    },
    OtherRequest.STATUS_REWORK: {
        OtherRequest.STATUS_IN_PROGRESS,
        OtherRequest.STATUS_DONE_BY_DEPARTMENT,
        OtherRequest.STATUS_AWAITING_MANAGER_CHECK,
        OtherRequest.STATUS_CANCELLED,
    },
    OtherRequest.STATUS_CLOSED: {
        OtherRequest.STATUS_REWORK,
    },
    OtherRequest.STATUS_CANCELLED: set(),
    OtherRequest.STATUS_REJECTED: set(),
}

MANAGER_ROLES = frozenset({"manager", "head_manager", "director", "admin", "developer"})
MANAGER_CANCEL_LOCKED_STATUSES = frozenset(
    {
        OtherRequest.STATUS_WAREHOUSE_ACCEPTED,
        OtherRequest.STATUS_IN_PROGRESS,
        OtherRequest.STATUS_PAUSED,
        OtherRequest.STATUS_AWAITING_MANAGER_CHECK,
        OtherRequest.STATUS_DONE_BY_DEPARTMENT,
        OtherRequest.STATUS_REWORK,
    }
)
WAREHOUSE_CANCEL_REVIEW_ROLES = frozenset({"storekeeper", "processing_head"})
EXECUTOR_ROLES = frozenset(
    {
        "storekeeper",
        "processing_worker",
        "processing_head",
        "picker",
        "manager",
        "head_manager",
        "director",
        "admin",
        "developer",
    }
)

DEFAULT_CATEGORY_SEED = [
    ("remark", "Перемаркировка", "warehouse", 24, True, False, True),
    ("recount", "Пересчет товара", "warehouse", 24, False, True, True),
    ("find_goods", "Поиск товара", "warehouse", 12, False, False, False),
    ("defect_check", "Проверка брака", "warehouse", 24, True, True, True),
    ("photo", "Фото товара", "warehouse", 12, True, False, True),
    ("inventory", "Инвентаризация", "warehouse", 48, True, True, True),
    ("stock_check", "Проверка остатков", "warehouse", 12, False, False, False),
    ("repack", "Изменение упаковки", "packing", 24, False, True, True),
    ("documents", "Подготовка документов", "managers", 24, False, True, False),
    ("move_goods", "Перемещение товара", "warehouse", 24, False, True, True),
    ("marking_check", "Проверка маркировки", "warehouse", 24, True, False, True),
    ("clarify", "Уточнение данных", "managers", 24, False, False, False),
    ("measure_size", "Измерить размер", "warehouse", 24, False, False, True),
    ("courier_box", "Подготовить короб курьеру", "packing", 12, False, False, True),
    ("photo_video", "Фото / видео товара", "warehouse", 12, True, False, True),
    ("custom", "Другое", "warehouse", 24, False, False, True),
]


RESULT_KIND_MEASURE = "measure"
RESULT_KIND_PHOTO = "photo"
RESULT_KIND_RECOUNT = "recount"
RESULT_KIND_CHECK = "check"
RESULT_KIND_GENERIC = "generic"

# Категория → тип формы результата. Без новых полей/миграций: только выбор UI-сценария
# и способ упаковки данных в существующие result_* поля.
CATEGORY_RESULT_KIND = {
    "measure_size": RESULT_KIND_MEASURE,
    "photo": RESULT_KIND_PHOTO,
    "photo_video": RESULT_KIND_PHOTO,
    "recount": RESULT_KIND_RECOUNT,
    "inventory": RESULT_KIND_RECOUNT,
    "defect_check": RESULT_KIND_CHECK,
    "marking_check": RESULT_KIND_CHECK,
    "stock_check": RESULT_KIND_CHECK,
}

RESULT_OUTCOME_LABELS = {
    "done_full": "Выполнено полностью",
    "done_partial": "Выполнено частично",
    "impossible": "Невозможно выполнить",
    "mismatch": "Обнаружено расхождение",
    "need_manager": "Требуется решение менеджера",
}


def result_form_kind(category_code: str) -> str:
    return CATEGORY_RESULT_KIND.get(str(category_code or "").strip(), RESULT_KIND_GENERIC)


def result_outcome_label(code: str) -> str:
    code = str(code or "")
    return RESULT_OUTCOME_LABELS.get(code, code or "—")


class OtherRequestError(Exception):
    def __init__(self, message: str, *, code: str = "error"):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_result_requirements(request_obj: OtherRequest, *, result_qty=None) -> list[str]:
    """Возвращает список недостающих обязательных данных перед завершением.

    Работает только на существующих полях/вложениях: require_qty → result_qty,
    require_result_photo/require_result_file → вложение с purpose=result.
    """
    missing: list[str] = []
    category = request_obj.category
    if not category:
        return missing
    if category.require_qty:
        qty_value = result_qty if result_qty is not None else request_obj.result_qty
        if qty_value in (None, ""):
            missing.append("фактическое количество")
    if category.require_result_photo or category.require_result_file:
        from .models import OtherRequestAttachment

        result_count = OtherRequestAttachment.objects.filter(
            request=request_obj,
            purpose=OtherRequestAttachment.PURPOSE_RESULT,
        ).count()
        if result_count < 1:
            missing.append("фото результата" if category.require_result_photo else "файл результата")
    return missing


def seed_default_categories() -> int:
    created = 0
    for idx, (code, title, dept, sla, photo, qty, paid) in enumerate(DEFAULT_CATEGORY_SEED, start=1):
        _, was_created = OtherRequestCategory.objects.get_or_create(
            code=code,
            defaults={
                "title": title,
                "default_department": dept,
                "default_sla_hours": sla,
                "require_result_photo": photo,
                "require_qty": qty,
                "can_be_paid": paid,
                "sort_order": idx * 10,
                "is_active": True,
            },
        )
        if was_created:
            created += 1
    return created


def resolve_category(category_code: str) -> OtherRequestCategory | None:
    code = str(category_code or "").strip() or "custom"
    aliases = {"photo_report": "photo", "other": "custom", "photo_video": "photo_video"}
    code = aliases.get(code, code)
    cat = OtherRequestCategory.objects.filter(code=code, is_active=True).first()
    if cat:
        return cat
    return OtherRequestCategory.objects.filter(code="custom").first()


def _employee_for_user(user) -> Employee | None:
    if not user or not getattr(user, "is_authenticated", False):
        return None
    return Employee.objects.filter(user=user, is_active=True).first()


def _role(user) -> str:
    emp = _employee_for_user(user)
    return str(getattr(emp, "role", "") or "")


def get_other_request(public_number: str) -> OtherRequest | None:
    return (
        OtherRequest.objects.select_related("agency", "category", "manager", "assignee", "created_by")
        .filter(public_number=str(public_number or "").strip())
        .first()
    )


def _executor_task_route(request_obj: OtherRequest) -> str:
    return f"/orders/other/{request_obj.public_number}/"


def _executor_tasks(request_obj: OtherRequest):
    """Open warehouse execution tasks linked to this other request."""
    return Task.objects.filter(
        route=_executor_task_route(request_obj),
        title__startswith="Выполнить прочую заявку",
    ).exclude(status="done")


def _sync_executor_task_meta(
    request_obj: OtherRequest,
    *,
    assignee=None,
    sync_assignee: bool = False,
    due_at=None,
    sync_due_at: bool = False,
    priority: str = "",
    sync_priority: bool = False,
) -> int:
    """Keep the actionable todo task consistent with manager-edited request metadata."""
    updates: dict[str, Any] = {"updated_at": timezone.now()}
    if sync_assignee:
        updates["assigned_to"] = assignee
    # Task.due_date is non-nullable, so clearing the request deadline keeps the
    # existing task deadline instead of writing an invalid NULL.
    if sync_due_at and due_at is not None:
        updates["due_date"] = due_at
    if sync_priority and priority:
        updates["priority"] = priority
    if len(updates) == 1:
        return 0
    return _executor_tasks(request_obj).update(**updates)


def _assert_storekeeper_assignment(
    request_obj: OtherRequest,
    *,
    employee: Employee | None,
    role: str,
) -> None:
    if role != "storekeeper":
        return
    if employee is None:
        raise OtherRequestError("Для пользователя не найден активный сотрудник", code="forbidden")
    if request_obj.assignee_id and request_obj.assignee_id != employee.id:
        owner = getattr(request_obj.assignee, "full_name", "") or "другому сотруднику"
        raise OtherRequestError(f"Заявка уже назначена: {owner}", code="already_taken")
    task = (
        _executor_tasks(request_obj)
        .select_related("assigned_to")
        .order_by("-created_at")
        .first()
    )
    if task and task.assigned_to_id and task.assigned_to_id != employee.id:
        owner = getattr(task.assigned_to, "full_name", "") or "другому сотруднику"
        raise OtherRequestError(f"Задача назначена: {owner}", code="already_taken")


def _set_executor_task_status(
    request_obj: OtherRequest,
    *,
    employee: Employee | None,
    status: str,
) -> int:
    if employee is None:
        return 0
    return _executor_tasks(request_obj).update(
        assigned_to=employee,
        status=status,
        updated_at=timezone.now(),
    )


def ensure_other_request_from_audit(order_id: str) -> OtherRequest | None:
    """Создать/обновить OtherRequest из последнего audit, если записи ещё нет."""
    from audit.models import OrderAuditEntry

    order_id = str(order_id or "").strip()
    if not order_id:
        return None
    existing = get_other_request(order_id)
    latest = latest_other_entry(order_id)
    if not latest or not latest.agency_id:
        return existing
    payload = dict(latest.payload or {})
    legacy = str(payload.get("status") or "").lower()
    new_status = LEGACY_TO_NEW_STATUS.get(legacy, OtherRequest.STATUS_AWAITING_MANAGER)
    title = str(payload.get("title") or payload.get("order_title") or "").strip()
    description = str(payload.get("description") or "").strip()
    category_code = str(payload.get("category") or "custom").strip() or "custom"
    category = resolve_category(category_code)
    created_entry = (
        OrderAuditEntry.objects.filter(order_type=ORDER_TYPE, order_id=order_id)
        .order_by("created_at")
        .first()
    )
    defaults = {
        "agency": latest.agency,
        "category": category,
        "category_code": category.code if category else category_code,
        "title": title[:255],
        "description": description,
        "status": new_status,
        "department": (category.default_department if category else OtherRequest.DEPARTMENT_WAREHOUSE)
        or OtherRequest.DEPARTMENT_WAREHOUSE,
        "priority": OtherRequest.PRIORITY_NORMAL,
        "created_by": getattr(created_entry, "user", None) if created_entry else None,
        "due_at": timezone.now() + timedelta(hours=int(getattr(category, "default_sla_hours", 24) or 24)),
    }
    if existing:
        changed = False
        for key, value in defaults.items():
            if key in {"status", "title", "description"} and not getattr(existing, key):
                setattr(existing, key, value)
                changed = True
        if changed:
            existing.save()
        return existing
    obj = OtherRequest.objects.create(public_number=order_id, **defaults)
    return obj


def _sync_audit(
    request_obj: OtherRequest,
    *,
    user=None,
    description: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    legacy = NEW_TO_LEGACY_STATUS.get(request_obj.status, "submitted")
    payload_extra = {
        "workflow_status": request_obj.status,
        "workflow_status_label": request_obj.status_label,
        "title": request_obj.display_title,
        "category": request_obj.category_code or (request_obj.category.code if request_obj.category_id else "custom"),
        "category_label": (
            request_obj.category.title
            if request_obj.category_id
            else request_obj.category_code or "Другое"
        ),
        "description": request_obj.description,
        "department": request_obj.department,
        "priority": request_obj.priority,
        "assignee_id": request_obj.assignee_id,
        "due_at": request_obj.due_at.isoformat() if request_obj.due_at else "",
        "cancel_reason": request_obj.cancel_reason,
        "reject_reason": request_obj.reject_reason,
        "rework_reason": request_obj.rework_reason,
        "pause_reason": request_obj.pause_reason,
        "result_outcome": request_obj.result_outcome,
        "result_comment": request_obj.result_comment,
    }
    if extra:
        payload_extra.update(extra)
    # Если audit ещё нет — создаём через set_other_status только при наличии latest.
    if latest_other_entry(request_obj.public_number):
        set_other_status(
            order_id=request_obj.public_number,
            status=legacy,
            user=user,
            description=description or f"Статус: {request_obj.status_label}",
            extra=payload_extra,
        )
    else:
        from audit.models import log_order_action

        log_order_action(
            "status",
            order_id=request_obj.public_number,
            order_type=ORDER_TYPE,
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=request_obj.agency,
            description=description or f"Статус: {request_obj.status_label}",
            payload={
                "status": legacy,
                "status_label": request_obj.status_label,
                "submit_action": legacy,
                **payload_extra,
            },
        )


def _assert_transition(request_obj: OtherRequest, new_status: str) -> None:
    allowed = ALLOWED_TRANSITIONS.get(request_obj.status, set())
    if new_status == request_obj.status:
        return
    if new_status not in allowed:
        raise OtherRequestError(
            f"Переход «{request_obj.status_label}» → «{dict(OtherRequest.STATUS_CHOICES).get(new_status, new_status)}» запрещён",
            code="invalid_transition",
        )


@transaction.atomic
def transition(
    request_obj: OtherRequest,
    *,
    new_status: str,
    user=None,
    description: str = "",
    extra_fields: dict[str, Any] | None = None,
    audit_extra: dict[str, Any] | None = None,
) -> OtherRequest:
    locked = OtherRequest.objects.select_for_update().get(pk=request_obj.pk)
    _assert_transition(locked, new_status)
    locked.status = new_status
    if extra_fields:
        for key, value in extra_fields.items():
            setattr(locked, key, value)
    locked.save()
    _sync_audit(locked, user=user, description=description, extra=audit_extra)
    return locked


def approve_and_send_to_department(
    request_obj: OtherRequest,
    *,
    user=None,
    department: str | None = None,
) -> OtherRequest:
    role = _role(user)
    if role not in MANAGER_ROLES:
        raise OtherRequestError("Недостаточно прав для передачи в подразделение", code="forbidden")
    dept = department or request_obj.department or OtherRequest.DEPARTMENT_WAREHOUSE
    obj = transition(
        request_obj,
        new_status=OtherRequest.STATUS_AWAITING_DEPARTMENT,
        user=user,
        description="Подтверждено менеджером и передано в подразделение",
        extra_fields={"department": dept},
    )
    close_open_tasks(order_id=obj.public_number, role="manager")
    # Пока операционный исполнитель для склада/упаковки — кладовщик (todo Task).
    if dept in {
        OtherRequest.DEPARTMENT_WAREHOUSE,
        OtherRequest.DEPARTMENT_PACKING,
        OtherRequest.DEPARTMENT_PROCESSING,
    }:
        observer = _employee_for_user(user)
        create_storekeeper_task(
            order_id=obj.public_number,
            agency=obj.agency,
            user=user,
            category=obj.category_code or "custom",
            observer=observer,
        )
        _sync_executor_task_meta(
            obj,
            assignee=obj.assignee,
            sync_assignee=bool(obj.assignee_id),
            due_at=obj.due_at,
            sync_due_at=bool(obj.due_at),
            priority=obj.priority,
            sync_priority=True,
        )
    return obj


def accept_by_warehouse(request_obj: OtherRequest, *, user=None) -> OtherRequest:
    role = _role(user)
    if role not in EXECUTOR_ROLES:
        raise OtherRequestError("Недостаточно прав, чтобы принять заявку складом", code="forbidden")
    emp = _employee_for_user(user)
    with transaction.atomic():
        locked = OtherRequest.objects.select_for_update().get(pk=request_obj.pk)
        _assert_storekeeper_assignment(locked, employee=emp, role=role)
        if locked.status == OtherRequest.STATUS_WAREHOUSE_ACCEPTED and locked.assignee_id == getattr(emp, "id", None):
            return locked
        if locked.status != OtherRequest.STATUS_AWAITING_DEPARTMENT:
            raise OtherRequestError("Принять можно только заявку, переданную на склад", code="invalid_transition")
        locked.status = OtherRequest.STATUS_WAREHOUSE_ACCEPTED
        locked.assignee = emp
        locked.save(update_fields=["status", "assignee", "updated_at"])
        _sync_audit(
            locked,
            user=user,
            description="Заявка принята складом",
        )
    return locked


def take_in_progress(request_obj: OtherRequest, *, user=None) -> OtherRequest:
    role = _role(user)
    if role not in EXECUTOR_ROLES:
        raise OtherRequestError("Недостаточно прав, чтобы взять заявку в работу", code="forbidden")
    emp = _employee_for_user(user)
    with transaction.atomic():
        locked = OtherRequest.objects.select_for_update().get(pk=request_obj.pk)
        _assert_storekeeper_assignment(locked, employee=emp, role=role)
        if locked.status not in {
            OtherRequest.STATUS_WAREHOUSE_ACCEPTED,
            OtherRequest.STATUS_REWORK,
        }:
            if locked.status == OtherRequest.STATUS_IN_PROGRESS and locked.assignee_id == getattr(emp, "id", None):
                _set_executor_task_status(locked, employee=emp, status="in_progress")
                return locked
            raise OtherRequestError("Заявку нельзя взять в работу в текущем статусе", code="invalid_transition")
        locked.status = OtherRequest.STATUS_IN_PROGRESS
        locked.assignee = emp
        locked.started_at = locked.started_at or timezone.now()
        locked.save(
            update_fields=["status", "assignee", "started_at", "updated_at"]
        )
        _sync_audit(
            locked,
            user=user,
            description="Заявка взята в работу",
        )
        _set_executor_task_status(locked, employee=emp, status="in_progress")
    return locked


@transaction.atomic
def start_by_executor(request_obj: OtherRequest, *, user=None) -> OtherRequest:
    """Одним действием принять новую заявку и перевести ее в работу."""
    locked = OtherRequest.objects.select_for_update().get(pk=request_obj.pk)
    if locked.status == OtherRequest.STATUS_AWAITING_DEPARTMENT:
        locked = accept_by_warehouse(locked, user=user)
    return take_in_progress(locked, user=user)


@transaction.atomic
@transaction.atomic
def complete_by_executor(
    request_obj: OtherRequest,
    *,
    user=None,
    result_outcome: str = "done_full",
    result_comment: str = "",
    result_qty=None,
    result_unit: str = "",
) -> OtherRequest:
    role = _role(user)
    if role not in EXECUTOR_ROLES:
        raise OtherRequestError("Недостаточно прав для завершения работы", code="forbidden")
    emp = _employee_for_user(user)
    locked = OtherRequest.objects.select_for_update().get(pk=request_obj.pk)
    if locked.status != OtherRequest.STATUS_IN_PROGRESS:
        raise OtherRequestError("Завершить можно только заявку в работе", code="invalid_transition")
    if not str(result_comment or locked.result_comment or "").strip():
        raise OtherRequestError("Заполните, что было сделано", code="result_required")
    if locked.assignee_id and emp and locked.assignee_id != emp.id and role not in MANAGER_ROLES:
        raise OtherRequestError("Заявку может завершить только ответственный", code="forbidden")
    missing = validate_result_requirements(locked, result_qty=result_qty)
    if missing:
        raise OtherRequestError(
            "Нельзя завершить заявку. Не заполнены: " + ", ".join(missing) + ".",
            code="missing_result",
        )
    from billing.models import BillingApplication
    from billing.warehouse_services import require_completion_facts

    try:
        require_completion_facts(
            client=locked.agency,
            order_type=BillingApplication.TYPE_OTHER,
            order_id=locked.public_number,
        )
    except ValidationError as exc:
        raise OtherRequestError("; ".join(exc.messages), code="services_required") from exc
    now = timezone.now()
    completed = transition(
        locked,
        new_status=OtherRequest.STATUS_AWAITING_MANAGER_CHECK,
        user=user,
        description="Работа выполнена, ожидает проверки менеджером",
        extra_fields={
            "result_outcome": result_outcome,
            "result_comment": result_comment or locked.result_comment,
            "result_qty": result_qty if result_qty is not None else locked.result_qty,
            "result_unit": result_unit or locked.result_unit or "",
            "completed_at": now,
        },
    )
    _set_executor_task_status(completed, employee=emp, status="done")
    return completed


def save_executor_result(
    request_obj: OtherRequest,
    *,
    user=None,
    result_outcome: str = "",
    result_comment: str = "",
    result_qty=None,
    result_unit: str = "",
    services_text: str = "",
) -> OtherRequest:
    role = _role(user)
    if role not in EXECUTOR_ROLES:
        raise OtherRequestError("Недостаточно прав для сохранения результата", code="forbidden")
    if request_obj.status not in {
        OtherRequest.STATUS_IN_PROGRESS,
        OtherRequest.STATUS_REWORK,
        OtherRequest.STATUS_WAREHOUSE_ACCEPTED,
    }:
        raise OtherRequestError("Результат можно сохранять только в работе склада", code="invalid_transition")
    with transaction.atomic():
        locked = OtherRequest.objects.select_for_update().get(pk=request_obj.pk)
        locked.result_outcome = result_outcome or locked.result_outcome or "done_full"
        locked.result_comment = result_comment or locked.result_comment
        locked.result_qty = result_qty if result_qty is not None else locked.result_qty
        locked.result_unit = result_unit or locked.result_unit
        if services_text:
            locked.internal_note = services_text
        locked.save(
            update_fields=[
                "result_outcome",
                "result_comment",
                "result_qty",
                "result_unit",
                "internal_note",
                "updated_at",
            ]
        )
        _sync_audit(
            locked,
            user=user,
            description="Склад сохранил результат выполнения",
            extra={
                "services_text": services_text or locked.internal_note,
            },
        )
    return locked


def request_manager_clarification(request_obj: OtherRequest, *, user=None, reason: str = "") -> OtherRequest:
    role = _role(user)
    if role not in EXECUTOR_ROLES:
        raise OtherRequestError("Недостаточно прав для запроса уточнения", code="forbidden")
    if not str(reason or "").strip():
        raise OtherRequestError("Укажите, что нужно уточнить", code="reason_required")
    if request_obj.status not in {
        OtherRequest.STATUS_WAREHOUSE_ACCEPTED,
        OtherRequest.STATUS_IN_PROGRESS,
        OtherRequest.STATUS_REWORK,
    }:
        raise OtherRequestError("Уточнение доступно только в складской работе", code="invalid_transition")
    return transition(
        request_obj,
        new_status=OtherRequest.STATUS_PAUSED,
        user=user,
        description=f"Склад запросил уточнение у менеджера: {reason}",
        extra_fields={"pause_reason": reason},
        audit_extra={"clarification_request": reason},
    )


def ensure_billing_for_other_request(request_obj: OtherRequest, *, user=None):
    """Связывает факты другой заявки с биллингом без расчета начислений."""
    if not request_obj.agency_id:
        return None
    from billing.warehouse_services import sync_other_request_facts_to_billing

    app, _facts = sync_other_request_facts_to_billing(request_obj=request_obj, user=user)
    return app


@transaction.atomic
def approve_and_close(request_obj: OtherRequest, *, user=None) -> OtherRequest:
    role = _role(user)
    if role not in MANAGER_ROLES:
        raise OtherRequestError("Недостаточно прав для закрытия заявки", code="forbidden")
    obj = transition(
        request_obj,
        new_status=OtherRequest.STATUS_CLOSED,
        user=user,
        description="Менеджер подтвердил результат и закрыл заявку",
        extra_fields={"closed_at": timezone.now()},
    )
    try:
        ensure_billing_for_other_request(obj, user=user)
    except ValidationError as exc:
        raise OtherRequestError("; ".join(exc.messages), code="services_required") from exc
    close_open_tasks(order_id=obj.public_number)
    return obj


def client_accept_result(request_obj: OtherRequest, *, user=None) -> OtherRequest:
    """Клиент принял результат «Другой заявки».

    Статус остаётся закрытым: фиксируем принятие в audit, чтобы в ЛК больше не
    показывать действия «принять/вернуть».
    """
    if request_obj.status != OtherRequest.STATUS_CLOSED:
        raise OtherRequestError("Принять можно только выполненную заявку", code="invalid_transition")
    _sync_audit(
        request_obj,
        user=user,
        description="Клиент принял результат заявки",
        extra={
            "client_result_status": "accepted",
            "client_accepted_at": timezone.localtime().isoformat(),
            "status_label": "Выполнена, принята клиентом",
        },
    )
    return request_obj


def client_return_to_work(request_obj: OtherRequest, *, user=None, reason: str = "") -> OtherRequest:
    """Клиент вернул выполненную «Другую заявку» в работу без новых полей."""
    if request_obj.status != OtherRequest.STATUS_CLOSED:
        raise OtherRequestError("Вернуть в работу можно только выполненную заявку", code="invalid_transition")
    if not str(reason or "").strip():
        raise OtherRequestError("Укажите причину возврата в работу", code="reason_required")
    obj = transition(
        request_obj,
        new_status=OtherRequest.STATUS_REWORK,
        user=user,
        description=f"Клиент вернул заявку в работу: {reason}",
        extra_fields={
            "rework_reason": reason,
            "closed_at": None,
        },
        audit_extra={
            "client_result_status": "returned",
            "client_return_reason": reason,
            "client_returned_at": timezone.localtime().isoformat(),
        },
    )
    create_storekeeper_task(
        order_id=obj.public_number,
        agency=obj.agency,
        user=user,
        category=obj.category_code or "custom",
        observer=obj.manager,
    )
    return obj


def request_client_info(request_obj: OtherRequest, *, user=None, reason: str = "") -> OtherRequest:
    role = _role(user)
    if role not in MANAGER_ROLES:
        raise OtherRequestError("Недостаточно прав для запроса уточнения", code="forbidden")
    if not str(reason or "").strip():
        raise OtherRequestError("Укажите, что нужно уточнить у клиента", code="reason_required")
    return transition(
        request_obj,
        new_status=OtherRequest.STATUS_NEED_CLIENT_INFO,
        user=user,
        description=f"Требуется уточнение от клиента: {reason}",
        extra_fields={"internal_note": reason},
    )


def return_for_rework(request_obj: OtherRequest, *, user=None, reason: str = "") -> OtherRequest:
    role = _role(user)
    if role not in MANAGER_ROLES:
        raise OtherRequestError("Недостаточно прав для возврата на доработку", code="forbidden")
    if not str(reason or "").strip():
        raise OtherRequestError("Укажите причину возврата", code="reason_required")
    return transition(
        request_obj,
        new_status=OtherRequest.STATUS_REWORK,
        user=user,
        description=f"Возвращено на доработку: {reason}",
        extra_fields={"rework_reason": reason},
    )


def warehouse_cancel_request_payload(request_obj: OtherRequest) -> dict:
    from audit.models import OrderAuditEntry

    entries = (
        OrderAuditEntry.objects.filter(order_type=ORDER_TYPE, order_id=request_obj.public_number)
        .order_by("-created_at", "-id")
    )
    for entry in entries:
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


def _ensure_warehouse_cancel_review_task(
    request_obj: OtherRequest,
    *,
    user=None,
    reason: str,
) -> None:
    reviewer = (
        Employee.objects.filter(role="storekeeper", is_active=True).order_by("full_name").first()
        or Employee.objects.filter(role="processing_head", is_active=True).order_by("full_name").first()
    )
    if not reviewer:
        return
    route = request_obj.wms_url
    title = f"Подтвердите отмену другой заявки №{request_obj.public_number}"
    description = (
        f"Клиент: {request_obj.agency.agn_name or request_obj.agency.inn or request_obj.agency.id}"
        f"\nПричина отмены: {str(reason).strip()}"
    )
    task = (
        Task.objects.filter(route=route, assigned_to=reviewer, title=title)
        .exclude(status="done")
        .first()
    )
    if task:
        task.description = description
        task.due_date = timezone.localtime()
        task.save(update_fields=["description", "due_date", "updated_at"])
        return
    Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=reviewer,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=timezone.localtime(),
    )


def _close_warehouse_cancel_review_task(request_obj: OtherRequest) -> None:
    Task.objects.filter(
        route=request_obj.wms_url,
        title__startswith="Подтвердите отмену другой заявки",
    ).exclude(status="done").update(status="done")


def request_warehouse_cancel_confirmation(
    request_obj: OtherRequest,
    *,
    user=None,
    reason: str,
) -> OtherRequest:
    role = _role(user)
    if role not in MANAGER_ROLES:
        raise OtherRequestError("Запросить отмену может только менеджер", code="forbidden")
    if request_obj.status not in MANAGER_CANCEL_LOCKED_STATUSES:
        raise OtherRequestError(
            "Подтверждение склада для этой заявки не требуется",
            code="invalid_transition",
        )
    cancel_reason = str(reason or "").strip()
    if not cancel_reason:
        raise OtherRequestError("Укажите причину отмены", code="reason_required")
    if warehouse_cancel_request_payload(request_obj):
        return request_obj
    _ensure_warehouse_cancel_review_task(request_obj, user=user, reason=cancel_reason)
    latest = latest_other_entry(request_obj.public_number)
    payload = dict(getattr(latest, "payload", None) or {})
    payload.update(
        {
            "status": "warehouse_cancel_requested",
            "status_label": "Отмена ожидает подтверждения склада",
            "workflow_status": request_obj.status,
            "workflow_status_label": request_obj.status_label,
            "cancel_reason": cancel_reason,
            "cancel_requested_by_manager": True,
        }
    )
    log_order_action(
        "warehouse_cancel_request",
        order_id=request_obj.public_number,
        order_type=ORDER_TYPE,
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=request_obj.agency,
        description="Менеджер запросил подтверждение отмены у склада",
        payload=payload,
    )
    return request_obj


def reject_warehouse_cancel_confirmation(
    request_obj: OtherRequest,
    *,
    user=None,
) -> OtherRequest:
    if _role(user) not in WAREHOUSE_CANCEL_REVIEW_ROLES:
        raise OtherRequestError("Отклонить отмену может только склад", code="forbidden")
    if not warehouse_cancel_request_payload(request_obj):
        raise OtherRequestError("Активный запрос на отмену не найден", code="invalid_transition")
    _close_warehouse_cancel_review_task(request_obj)
    latest = latest_other_entry(request_obj.public_number)
    payload = dict(getattr(latest, "payload", None) or {})
    legacy = NEW_TO_LEGACY_STATUS.get(request_obj.status, "warehouse")
    payload.update(
        {
            "status": legacy,
            "status_label": request_obj.status_label,
            "submit_action": legacy,
            "workflow_status": request_obj.status,
            "workflow_status_label": request_obj.status_label,
            "cancel_request_rejected_by_warehouse": True,
        }
    )
    payload.pop("cancel_requested_by_manager", None)
    payload.pop("cancel_reason", None)
    log_order_action(
        "warehouse_cancel_rejected",
        order_id=request_obj.public_number,
        order_type=ORDER_TYPE,
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=request_obj.agency,
        description="Склад не подтвердил отмену; заявка остаётся в работе",
        payload=payload,
    )
    return request_obj


def cancel_request(
    request_obj: OtherRequest,
    *,
    user=None,
    reason: str = "",
    warehouse_approved: bool = False,
) -> OtherRequest:
    role = _role(user)
    if request_obj.status in MANAGER_CANCEL_LOCKED_STATUSES and not warehouse_approved:
        raise OtherRequestError(
            "Заявка уже передана на склад. Менеджер не может отменить её самостоятельно.",
            code="forbidden",
        )
    if warehouse_approved and role not in WAREHOUSE_CANCEL_REVIEW_ROLES:
        raise OtherRequestError("Подтвердить отмену может только склад", code="forbidden")
    if warehouse_approved and not warehouse_cancel_request_payload(request_obj):
        raise OtherRequestError("Активный запрос на отмену не найден", code="invalid_transition")
    if not str(reason or "").strip() and request_obj.status not in {OtherRequest.STATUS_DRAFT}:
        raise OtherRequestError("Укажите причину отмены", code="reason_required")
    obj = transition(
        request_obj,
        new_status=OtherRequest.STATUS_CANCELLED,
        user=user,
        description=(
            f"Склад подтвердил отмену заявки. Причина: {reason}"
            if warehouse_approved
            else reason or "Заявка отменена"
        ),
        extra_fields={
            "cancel_reason": reason or "",
            "cancelled_at": timezone.now(),
        },
        audit_extra={"cancelled_by": "warehouse" if warehouse_approved else role or "user"},
    )
    _close_warehouse_cancel_review_task(obj)
    close_open_tasks(order_id=obj.public_number)
    return obj


def update_meta(
    request_obj: OtherRequest,
    *,
    user=None,
    assignee=None,
    assignee_provided: bool = False,
    department: str = "",
    priority: str = "",
    due_at=None,
    due_at_provided: bool = False,
) -> OtherRequest:
    """Точечное редактирование сводки менеджером: ответственный/подразделение/приоритет/срок.

    Использует только существующие поля OtherRequest, изменения фиксируются в audit.
    """
    role = _role(user)
    if role not in MANAGER_ROLES:
        raise OtherRequestError("Недостаточно прав для изменения параметров заявки", code="forbidden")
    changes: list[str] = []
    with transaction.atomic():
        locked = OtherRequest.objects.select_for_update().get(pk=request_obj.pk)
        update_fields: list[str] = []
        sync_assignee = False
        sync_due_at = False
        sync_priority = False
        if assignee_provided and assignee != locked.assignee:
            old = locked.assignee.full_name if locked.assignee else "не назначен"
            new = assignee.full_name if assignee else "не назначен"
            changes.append(f"ответственный: {old} → {new}")
            locked.assignee = assignee
            update_fields.append("assignee")
            sync_assignee = True
        if department and department != locked.department:
            changes.append(
                f"подразделение: {locked.department_label} → "
                f"{dict(OtherRequest.DEPARTMENT_CHOICES).get(department, department)}"
            )
            locked.department = department
            update_fields.append("department")
        if priority and priority != locked.priority:
            changes.append(
                f"приоритет: {locked.priority_label} → "
                f"{dict(OtherRequest.PRIORITY_CHOICES).get(priority, priority)}"
            )
            locked.priority = priority
            update_fields.append("priority")
            sync_priority = True
        if due_at_provided and due_at != locked.due_at:
            changes.append("срок изменён")
            locked.due_at = due_at
            update_fields.append("due_at")
            sync_due_at = True
        if update_fields:
            locked.save(update_fields=update_fields + ["updated_at"])
            _sync_executor_task_meta(
                locked,
                assignee=locked.assignee,
                sync_assignee=sync_assignee,
                due_at=locked.due_at,
                sync_due_at=sync_due_at,
                priority=locked.priority,
                sync_priority=sync_priority,
            )
            _sync_audit(locked, user=user, description="; ".join(changes) or "Изменены параметры заявки")
    return locked


@transaction.atomic
def upsert_from_create(
    *,
    public_number: str,
    agency,
    user=None,
    category: str = "custom",
    description: str = "",
    title: str = "",
    save_as_draft: bool = False,
) -> OtherRequest:
    seed_default_categories()
    cat = resolve_category(category)
    status = OtherRequest.STATUS_DRAFT if save_as_draft else OtherRequest.STATUS_AWAITING_MANAGER
    due = timezone.now() + timedelta(hours=int(getattr(cat, "default_sla_hours", 24) or 24))
    from .other_requests import resolve_client_manager

    manager = resolve_client_manager(agency)
    existing = (
        OtherRequest.objects.select_for_update()
        .filter(public_number=public_number)
        .only("id", "agency_id")
        .first()
    )
    if existing is not None and existing.agency_id != getattr(agency, "id", None):
        raise OtherRequestError("Номер заявки уже используется", code="number_conflict")
    obj, _created = OtherRequest.objects.update_or_create(
        public_number=public_number,
        agency=agency,
        defaults={
            "category": cat,
            "category_code": cat.code if cat else (category or "custom"),
            "title": (title or "")[:255],
            "description": description or "",
            "status": status,
            "department": (cat.default_department if cat else OtherRequest.DEPARTMENT_WAREHOUSE)
            or OtherRequest.DEPARTMENT_WAREHOUSE,
            "priority": OtherRequest.PRIORITY_NORMAL,
            "created_by": user if getattr(user, "is_authenticated", False) else None,
            "manager": manager,
            "due_at": due,
            "is_paid": bool(getattr(cat, "can_be_paid", False)) if cat else False,
        },
    )
    return obj
