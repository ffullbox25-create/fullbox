from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from audit.models import OrderAuditEntry, log_order_action
from employees.models import Employee
from fullbox.order_numbers import next_public_order_number
from todo.models import Task

ORDER_TYPE = "other"
OTHER_ROUTE_RE = re.compile(r"/orders/other/([^/]+)/")
OTHER_NUMBER_LOCK_NAME = "fullbox.client_cabinet.other_request.number"

OTHER_REQUEST_CATEGORIES = [
    {"id": "measure_size", "label": "Измерить размер"},
    {"id": "courier_box", "label": "Подготовить короб курьеру"},
    {"id": "photo_video", "label": "Фото / видео товара"},
    {"id": "repack", "label": "Переупаковка"},
    {"id": "custom", "label": "Другое"},
]

_CATEGORY_BY_ID = {item["id"]: item["label"] for item in OTHER_REQUEST_CATEGORIES}

# Aliases from older SPA / API stubs
_CATEGORY_ALIASES = {
    "photo_report": "photo_video",
    "other": "custom",
}

STATUS_LABELS = {
    "draft": "Черновик",
    "submitted": "Ждет подтверждения",
    "warehouse": "Ожидает склад",
    "warehouse_accepted": "Принято складом",
    "in_work": "Взята в работу",
    "completed": "Выполнена",
    "done": "Выполнена",
    "cancelled": "Отменена",
}


def category_label(category_id: str) -> str:
    raw = str(category_id or "").strip()
    key = _CATEGORY_ALIASES.get(raw, raw)
    return _CATEGORY_BY_ID.get(key, "Другое")


def normalize_category(category_id: str) -> str:
    raw = str(category_id or "").strip()
    key = _CATEGORY_ALIASES.get(raw, raw)
    if key in _CATEGORY_BY_ID:
        return key
    return "custom"


def extract_other_order_id(route: str | None) -> str | None:
    if not route:
        return None
    match = OTHER_ROUTE_RE.search(route)
    return match.group(1) if match else None


def other_detail_url(order_id: str, *, client_id: int | None = None) -> str:
    url = f"/orders/other/{order_id}/"
    if client_id:
        return f"{url}?client={client_id}"
    return url


def next_other_order_id() -> str:
    order_ids = (
        OrderAuditEntry.objects.filter(order_type=ORDER_TYPE)
        .values_list("order_id", flat=True)
        .distinct()
    )
    return next_public_order_number(ORDER_TYPE, order_ids)


def _next_other_order_id_locked() -> str:
    """Serialize production number allocation inside the surrounding transaction."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                [OTHER_NUMBER_LOCK_NAME],
            )
    return next_other_order_id()


def latest_other_entry(order_id: str) -> OrderAuditEntry | None:
    return (
        OrderAuditEntry.objects.filter(order_type=ORDER_TYPE, order_id=str(order_id))
        .select_related("agency", "user")
        .order_by("-created_at")
        .first()
    )


def _manager_due_date(submitted_at):
    try:
        return submitted_at + timedelta(days=1)
    except Exception:
        return timezone.localtime() + timedelta(days=1)


def resolve_client_manager(agency) -> Employee | None:
    """Prefer Agency.mened_user_id (legacy manager), then first active manager."""
    managers = Employee.objects.filter(role="manager", is_active=True)
    mened_id = getattr(agency, "mened_user_id", None) if agency is not None else None
    try:
        mened_id = int(mened_id) if mened_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        mened_id = None
    if mened_id:
        by_user = managers.filter(user_id=mened_id).order_by("full_name").first()
        if by_user:
            return by_user
        by_pk = managers.filter(pk=mened_id).order_by("full_name").first()
        if by_pk:
            return by_pk
    return managers.order_by("full_name").first()


def create_manager_task(*, order_id: str, agency, user, category: str, submitted_at=None) -> Task | None:
    manager = resolve_client_manager(agency)
    if not manager:
        return None
    submitted_at = submitted_at or timezone.localtime()
    label = category_label(category)
    client_name = getattr(agency, "agn_name", None) or getattr(agency, "inn", None) or agency.id
    route = other_detail_url(order_id)
    existing = (
        Task.objects.select_for_update()
        .filter(route=route, assigned_to=manager)
        .exclude(status="done")
        .order_by("id")
        .first()
    )
    if existing is not None:
        return existing
    return Task.objects.create(
        title=f"Подтвердите прочую заявку №{order_id} · {label}",
        description=f"Клиент: {client_name}",
        route=route,
        assigned_to=manager,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=_manager_due_date(submitted_at),
    )


def create_storekeeper_task(*, order_id: str, agency, user, category: str, observer=None) -> Task | None:
    storekeeper = (
        Employee.objects.filter(role="storekeeper", is_active=True)
        .order_by("full_name")
        .first()
    )
    if not storekeeper:
        return None
    label = category_label(category)
    client_name = getattr(agency, "agn_name", None) or getattr(agency, "inn", None) or agency.id
    return Task.objects.create(
        title=f"Выполнить прочую заявку №{order_id} · {label}",
        description=f"Клиент: {client_name}",
        route=other_detail_url(order_id),
        assigned_to=storekeeper,
        observer=observer,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=timezone.localtime() + timedelta(days=1),
    )


def close_open_tasks(*, order_id: str, role: str | None = None) -> int:
    qs = Task.objects.filter(route=other_detail_url(order_id)).exclude(status="done")
    if role:
        qs = qs.filter(assigned_to__role=role)
    return qs.update(status="done")


def build_payload(
    *,
    category: str,
    description: str,
    status: str,
    source: str = "other_form",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cat = normalize_category(category)
    payload = {
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "submit_action": status,
        "category": cat,
        "category_label": category_label(cat),
        "description": (description or "").strip(),
        "source": source or "other_form",
        "title": category_label(cat),
        "order_title": f"Прочая заявка · {category_label(cat)}",
        "order_display_title": f"Прочая заявка · {category_label(cat)}",
    }
    if extra:
        payload.update(extra)
    return payload


@transaction.atomic
def create_other_request(
    *,
    agency,
    user=None,
    category: str,
    description: str = "",
    save_as_draft: bool = False,
    source: str = "other_form",
    order_id: str | None = None,
) -> dict[str, Any]:
    from .client_drafts import (
        is_draft_payload,
        resolve_client_draft_order_id,
        supersede_extra_client_drafts,
    )

    cat = normalize_category(category)
    # The transaction-level company lock makes the one-draft rule safe when
    # two browser tabs autosave at the same moment.
    agency.__class__.objects.select_for_update().filter(pk=agency.pk).only("pk").first()
    status = "draft" if save_as_draft else "submitted"
    preferred_id = str(order_id or "").strip()
    promoted_draft_id = ""
    owned_draft = None
    if preferred_id:
        from .models import OtherRequest

        owned_draft = (
            OtherRequest.objects.select_for_update()
            .filter(
                public_number=preferred_id,
                agency=agency,
                status=OtherRequest.STATUS_DRAFT,
            )
            .first()
        )
        preferred_entry = (
            OrderAuditEntry.objects.select_for_update()
            .filter(order_type=ORDER_TYPE, order_id=preferred_id, agency=agency)
            .order_by("-created_at", "-id")
            .first()
        )
        if not preferred_entry or not is_draft_payload(preferred_entry.payload):
            owned_draft = None
        elif owned_draft is not None:
            from .request_ownership import (
                PORTAL_REQUEST_OWNER_MESSAGE,
                portal_user_can_edit_request,
            )

            if not portal_user_can_edit_request(
                user=user,
                agency=agency,
                order_type=ORDER_TYPE,
                order_id=preferred_id,
            ):
                raise PermissionError(PORTAL_REQUEST_OWNER_MESSAGE)
    if save_as_draft:
        if owned_draft is not None:
            order_id_value = owned_draft.public_number
        else:
            reusable_id = resolve_client_draft_order_id(
                agency=agency,
                order_type=ORDER_TYPE,
                preferred_order_id="",
                allow_new=True,
                user=user,
            )
            reusable_draft = None
            if reusable_id:
                from .models import OtherRequest

                reusable_draft = (
                    OtherRequest.objects.select_for_update()
                    .filter(
                        public_number=reusable_id,
                        agency=agency,
                        status=OtherRequest.STATUS_DRAFT,
                    )
                    .first()
                )
            order_id_value = (
                reusable_draft.public_number
                if reusable_draft is not None
                else _next_other_order_id_locked()
            )
    elif owned_draft is not None:
        promoted_draft_id = preferred_id
        order_id_value = _next_other_order_id_locked()
    else:
        order_id_value = _next_other_order_id_locked()
    now = timezone.localtime()
    payload = build_payload(
        category=cat,
        description=description,
        status=status,
        source=source,
        extra={"created_at": now.isoformat()},
    )
    action = "draft" if save_as_draft else "submit"
    existing = latest_other_entry(order_id_value)
    if existing:
        action = "update" if save_as_draft else "submit"
    log_order_action(
        action,
        order_id=order_id_value,
        order_type=ORDER_TYPE,
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=agency,
        description=f"{'Черновик' if save_as_draft else 'Создана'} прочая заявка · {category_label(cat)}",
        payload=payload,
    )
    if save_as_draft:
        supersede_extra_client_drafts(
            agency=agency,
            order_type=ORDER_TYPE,
            keep_order_id=order_id_value,
            user=user,
        )
    task = None
    if not save_as_draft:
        if promoted_draft_id:
            from .models import OtherRequestAttachment

            OtherRequestAttachment.objects.filter(order_id=promoted_draft_id, agency=agency).update(
                order_id=order_id_value
            )
            OrderAuditEntry.objects.filter(
                order_type=ORDER_TYPE,
                order_id=promoted_draft_id,
                agency=agency,
            ).delete()
        task = create_manager_task(
            order_id=order_id_value,
            agency=agency,
            user=user,
            category=cat,
            submitted_at=now,
        )
    from .other_request_workflow import upsert_from_create

    upsert_from_create(
        public_number=order_id_value,
        agency=agency,
        user=user,
        category=cat,
        description=description,
        title="",
        save_as_draft=save_as_draft,
    )
    return {
        "order_id": order_id_value,
        "order_type": ORDER_TYPE,
        "status": status,
        "status_label": payload["status_label"],
        "category": cat,
        "category_label": payload["category_label"],
        "detail_url": other_detail_url(order_id_value, client_id=getattr(agency, "id", None)),
        "manager_task_id": task.id if task else None,
    }


# Client LK should surface these warehouse-driven status jumps as notifications.
CLIENT_NOTIFY_STATUSES = frozenset({"warehouse", "in_work", "completed", "done", "cancelled"})


def set_other_status(
    *,
    order_id: str,
    status: str,
    user=None,
    description: str = "",
    extra: dict[str, Any] | None = None,
) -> OrderAuditEntry | None:
    latest = latest_other_entry(order_id)
    if not latest:
        return None
    payload = dict(latest.payload or {})
    payload["status"] = status
    payload["status_label"] = STATUS_LABELS.get(status, status)
    payload["submit_action"] = status
    if status in CLIENT_NOTIFY_STATUSES:
        payload["notify_client"] = True
    if extra:
        payload.update(extra)
    log_order_action(
        "status",
        order_id=str(order_id),
        order_type=ORDER_TYPE,
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=latest.agency,
        description=description or f"Статус: {payload['status_label']}",
        payload=payload,
    )
    entry = latest_other_entry(order_id)
    if entry and status in CLIENT_NOTIFY_STATUSES and entry.agency_id:
        try:
            from .messaging_lk import create_notification
            from .lk_requests import lk_request_hash
            from .client_visibility import client_safe_other_status_description

            create_notification(
                agency=entry.agency,
                title=str(payload.get("order_title") or payload.get("category_label") or f"Заявка {order_id}"),
                text=client_safe_other_status_description(status, description),
                notif_type="status",
                detail_url=lk_request_hash("other", order_id),
                priority="high",
                source_key=f"other-status:{order_id}:{status}:{entry.id}",
            )
        except Exception:
            pass
    return entry


def send_other_to_warehouse(task: Task, request) -> bool:
    order_id = extract_other_order_id(getattr(task, "route", None))
    if not order_id:
        return False
    latest = latest_other_entry(order_id)
    if not latest:
        return False
    try:
        from .other_request_workflow import (
            approve_and_send_to_department,
            ensure_other_request_from_audit,
        )

        req = ensure_other_request_from_audit(order_id)
        if req is not None:
            approve_and_send_to_department(req, user=getattr(request, "user", None))
            return True
    except Exception:
        pass
    payload = dict(latest.payload or {})
    status_value = str(payload.get("status") or "").lower()
    if status_value in {"warehouse", "in_work", "completed", "done", "cancelled"}:
        return True
    set_other_status(
        order_id=order_id,
        status="warehouse",
        user=getattr(request, "user", None),
        description="Подтверждено менеджером и отправлено на склад",
    )
    close_open_tasks(order_id=order_id, role="manager")
    observer = Employee.objects.filter(user=getattr(request, "user", None), is_active=True).first()
    create_storekeeper_task(
        order_id=order_id,
        agency=latest.agency,
        user=getattr(request, "user", None),
        category=str(payload.get("category") or "custom"),
        observer=observer,
    )
    return True


def take_other_in_work(*, order_id: str, user=None) -> bool:
    latest = latest_other_entry(order_id)
    if not latest:
        return False
    req = None
    try:
        from .other_request_workflow import ensure_other_request_from_audit, take_in_progress

        req = ensure_other_request_from_audit(order_id)
        if req is not None:
            take_in_progress(req, user=user)
            return True
    except Exception:
        if req is not None:
            return False
    status_value = str((latest.payload or {}).get("status") or "").lower()
    if status_value in {"in_work", "completed", "done", "cancelled"}:
        return True
    if status_value == "warehouse":
        return False
    set_other_status(
        order_id=order_id,
        status="in_work",
        user=user,
        description="Склад взял заявку в работу",
    )
    return True


def accept_other_by_warehouse(*, order_id: str, user=None) -> bool:
    latest = latest_other_entry(order_id)
    if not latest:
        return False
    try:
        from .other_request_workflow import accept_by_warehouse, ensure_other_request_from_audit

        req = ensure_other_request_from_audit(order_id)
        if req is not None:
            accept_by_warehouse(req, user=user)
            return True
    except Exception:
        pass
    payload = dict(latest.payload or {})
    if str(payload.get("status") or "").lower() != "warehouse":
        return False
    set_other_status(
        order_id=order_id,
        status="warehouse_accepted",
        user=user,
        description="Заявка принята складом",
    )
    return True


def complete_other_request(*, order_id: str, user=None) -> bool:
    latest = latest_other_entry(order_id)
    if not latest:
        return False
    req = None
    try:
        from .models import OtherRequest
        from .other_request_workflow import (
            approve_and_close,
            complete_by_executor,
            ensure_other_request_from_audit,
        )

        req = ensure_other_request_from_audit(order_id)
        if req is not None:
            if req.status == OtherRequest.STATUS_IN_PROGRESS:
                complete_by_executor(req, user=user)
                return True
            if req.status in {
                OtherRequest.STATUS_AWAITING_MANAGER_CHECK,
                OtherRequest.STATUS_DONE_BY_DEPARTMENT,
            }:
                approve_and_close(req, user=user)
                return True
            if req.status == OtherRequest.STATUS_CLOSED:
                return True
            # Нельзя завершать до принятия в работу — не падаем в legacy completed.
            if req.status in {
                OtherRequest.STATUS_AWAITING_DEPARTMENT,
                OtherRequest.STATUS_WAREHOUSE_ACCEPTED,
                OtherRequest.STATUS_AWAITING_MANAGER,
                OtherRequest.STATUS_APPROVED,
                OtherRequest.STATUS_REWORK,
            }:
                return False
    except Exception:
        if req is not None:
            return False
    set_other_status(
        order_id=order_id,
        status="completed",
        user=user,
        description="Прочая заявка выполнена",
    )
    close_open_tasks(order_id=order_id)
    return True


def complete_other_from_task(task: Task, request) -> bool:
    order_id = extract_other_order_id(getattr(task, "route", None))
    if not order_id:
        return False
    return complete_other_request(order_id=order_id, user=getattr(request, "user", None))


def cancel_other_request(*, order_id: str, user=None, reason: str = "") -> bool:
    latest = latest_other_entry(order_id)
    if not latest:
        return False
    from .other_request_workflow import (
        OtherRequestError,
        cancel_request,
        ensure_other_request_from_audit,
    )

    req = ensure_other_request_from_audit(order_id)
    if req is not None:
        cancel_request(req, user=user, reason=reason or "")
        return True
    payload = dict(latest.payload or {})
    if str(payload.get("status") or "").strip().lower() in {
        "warehouse_accepted",
        "in_work",
    } or str(payload.get("workflow_status") or "").strip().lower() in {
        "warehouse_accepted",
        "in_progress",
        "paused",
        "awaiting_manager_check",
        "done_by_department",
        "rework",
    }:
        raise OtherRequestError(
            "Заявка уже принята складом. Требуется подтверждение склада.",
            code="forbidden",
        )
    extra = {"cancel_reason": reason} if reason else None
    set_other_status(
        order_id=order_id,
        status="cancelled",
        user=user,
        description=reason or "Прочая заявка отменена",
        extra=extra,
    )
    close_open_tasks(order_id=order_id)
    return True


def list_other_entries(*, agency=None, limit: int = 100) -> list[OrderAuditEntry]:
    qs = OrderAuditEntry.objects.filter(order_type=ORDER_TYPE).select_related("agency", "user")
    if agency is not None:
        qs = qs.filter(agency=agency)
    latest_by_id: dict[str, OrderAuditEntry] = {}
    for entry in qs.order_by("-created_at")[: limit * 5]:
        if entry.order_id in latest_by_id:
            continue
        latest_by_id[entry.order_id] = entry
        if len(latest_by_id) >= limit:
            break
    return sorted(latest_by_id.values(), key=lambda item: item.created_at, reverse=True)


def save_other_attachments(
    *,
    order_id: str,
    agency,
    user=None,
    files=None,
    purpose: str = "client",
    request_obj=None,
) -> list[dict[str, Any]]:
    from .models import OtherRequest, OtherRequestAttachment

    purpose_value = purpose if purpose in {
        OtherRequestAttachment.PURPOSE_CLIENT,
        OtherRequestAttachment.PURPOSE_INTERNAL,
        OtherRequestAttachment.PURPOSE_RESULT,
    } else OtherRequestAttachment.PURPOSE_CLIENT
    linked = request_obj
    if linked is None:
        linked = OtherRequest.objects.filter(public_number=str(order_id)).first()

    saved: list[dict[str, Any]] = []
    for uploaded in files or []:
        if not uploaded or not getattr(uploaded, "name", ""):
            continue
        size = int(getattr(uploaded, "size", 0) or 0)
        attachment = OtherRequestAttachment.objects.create(
            order_id=str(order_id),
            agency=agency,
            request=linked,
            purpose=purpose_value,
            file_size=size,
            uploaded_by=user if getattr(user, "is_authenticated", False) else None,
            file=uploaded,
        )
        saved.append(
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "url": f"/client/api/v1/other-requests/attachments/{attachment.id}/",
                "uploaded_at": attachment.uploaded_at.isoformat() if attachment.uploaded_at else "",
                "purpose": attachment.purpose,
            }
        )
    if saved:
        latest = latest_other_entry(order_id)
        payload = dict((latest.payload if latest else {}) or {})
        names = list(payload.get("attachment_names") or [])
        names.extend(item["filename"] for item in saved)
        payload["attachment_names"] = names
        payload["attachments_count"] = len(names)
        log_order_action(
            "attachment",
            order_id=str(order_id),
            order_type=ORDER_TYPE,
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=agency,
            description=f"Добавлены файлы: {', '.join(item['filename'] for item in saved)}",
            payload=payload,
        )
    return saved


def list_other_attachments(*, order_id: str, agency) -> list[dict[str, Any]]:
    from .models import OtherRequestAttachment

    rows = []
    qs = (
        OtherRequestAttachment.objects.filter(order_id=str(order_id), agency=agency)
        .order_by("-uploaded_at")
    )
    purpose_labels = dict(OtherRequestAttachment.PURPOSE_CHOICES)
    for attachment in qs:
        rows.append(
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "url": f"/client/api/v1/other-requests/attachments/{attachment.id}/",
                "uploaded_at": attachment.uploaded_at.isoformat() if attachment.uploaded_at else "",
                "purpose": attachment.purpose,
                "purpose_label": purpose_labels.get(attachment.purpose, attachment.purpose),
                "file_size": attachment.file_size,
            }
        )
    return rows
