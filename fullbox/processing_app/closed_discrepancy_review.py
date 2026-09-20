from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.access import get_employee_for_user
from employees.models import Employee
from todo.models import Task

from .closed_discrepancy_audit import (
    audit_closed_processing_discrepancies,
    latest_closed_discrepancy_review,
)


REVIEW_STATUS_UNDER_REVIEW = "under_review"
REVIEW_STATUS_EXPLAINED = "explained"
REVIEW_STATUS_CORRECTION_REQUIRED = "correction_required"

REVIEW_STATUS_LABELS = {
    "": "Не разобрано",
    REVIEW_STATUS_UNDER_REVIEW: "Рассматривается",
    REVIEW_STATUS_EXPLAINED: "Расхождение объяснено",
    REVIEW_STATUS_CORRECTION_REQUIRED: "Требуется отдельная корректировка",
}

REVIEW_STATUS_CHOICES = tuple(
    (key, label)
    for key, label in REVIEW_STATUS_LABELS.items()
    if key
)

CORRECTION_TASK_TITLE_PREFIX = "Корректировка расхождения по обработке"
CORRECTION_TASK_ASSIGNEE_ROLES = ("processing_head", "processing_worker")


class ClosedDiscrepancyReviewError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ClosedDiscrepancyReviewResult:
    order_id: str
    status: str
    status_label: str
    comment: str
    created: bool


@dataclass(frozen=True, slots=True)
class ClosedDiscrepancyCorrectionTaskResult:
    order_id: str
    task_id: int
    assignee_name: str
    due_at: object
    created: bool
    updated: bool


def closed_discrepancy_correction_task_title(order_id: str) -> str:
    return f"{CORRECTION_TASK_TITLE_PREFIX} №{str(order_id or '').strip()}"


def closed_discrepancy_correction_task_route(order_id: str) -> str:
    return f"/orders/processing/{str(order_id or '').strip()}/work/"


def _reviewer_name(user) -> str:
    if not user or not getattr(user, "is_authenticated", False):
        return ""
    return str(user.get_full_name() or user.get_username() or user).strip()


def review_closed_processing_discrepancy(
    *,
    order_id: str,
    status: str,
    comment: str,
    user,
) -> ClosedDiscrepancyReviewResult:
    normalized_order_id = str(order_id or "").strip()
    normalized_status = str(status or "").strip().lower()
    normalized_comment = str(comment or "").strip()
    if not normalized_order_id:
        raise ClosedDiscrepancyReviewError("Не указан номер заявки.")
    if normalized_status not in REVIEW_STATUS_LABELS or not normalized_status:
        raise ClosedDiscrepancyReviewError("Выберите результат разбора.")
    if (
        normalized_status
        in {REVIEW_STATUS_EXPLAINED, REVIEW_STATUS_CORRECTION_REQUIRED}
        and not normalized_comment
    ):
        raise ClosedDiscrepancyReviewError(
            "Для итогового решения обязательно укажите причину."
        )

    with transaction.atomic():
        entries = list(
            OrderAuditEntry.objects.select_for_update(of=("self",))
            .filter(order_type="processing", order_id=normalized_order_id)
            .select_related("agency")
            .order_by("created_at", "id")
        )
        if not entries:
            raise ClosedDiscrepancyReviewError("Заявка на обработку не найдена.")
        report = audit_closed_processing_discrepancies(
            order_ids=[normalized_order_id],
            sample_limit=1,
        )
        if not report.rows:
            raise ClosedDiscrepancyReviewError(
                "Заявка не закрыта или расхождение количеств отсутствует."
            )

        discrepancy = report.rows[0]
        current_review = latest_closed_discrepancy_review(entries)
        reviewer_name = _reviewer_name(user)
        if (
            str(current_review.get("status") or "") == normalized_status
            and str(current_review.get("comment") or "").strip()
            == normalized_comment
        ):
            return ClosedDiscrepancyReviewResult(
                order_id=normalized_order_id,
                status=normalized_status,
                status_label=REVIEW_STATUS_LABELS[normalized_status],
                comment=normalized_comment,
                created=False,
            )

        reviewed_at = timezone.localtime().isoformat()
        review_payload = {
            "status": normalized_status,
            "status_label": REVIEW_STATUS_LABELS[normalized_status],
            "comment": normalized_comment,
            "reviewed_by": reviewer_name,
            "reviewed_at": reviewed_at,
            "source": "processing_head",
            "quantity_snapshot": {
                "declared_qty": discrepancy.declared_qty,
                "processed_qty": discrepancy.processed_qty,
                "boxed_qty": discrepancy.boxed_qty,
            },
        }
        OrderAuditEntry.objects.create(
            order_id=normalized_order_id,
            order_type="processing",
            action="update",
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=entries[-1].agency,
            description=(
                "Руководитель обработки обновил разбор расхождения: "
                f"{REVIEW_STATUS_LABELS[normalized_status]}"
            ),
            payload={
                "processing_discrepancy_review": review_payload,
                "internal_only": True,
            },
        )
        return ClosedDiscrepancyReviewResult(
            order_id=normalized_order_id,
            status=normalized_status,
            status_label=REVIEW_STATUS_LABELS[normalized_status],
            comment=normalized_comment,
            created=True,
        )


def create_or_update_closed_discrepancy_correction_task(
    *,
    order_id: str,
    assignee_id,
    due_at,
    comment: str,
    user,
) -> ClosedDiscrepancyCorrectionTaskResult:
    normalized_order_id = str(order_id or "").strip()
    normalized_comment = str(comment or "").strip()
    if not normalized_order_id:
        raise ClosedDiscrepancyReviewError("Не указан номер заявки.")
    if not normalized_comment:
        raise ClosedDiscrepancyReviewError(
            "Укажите, что требуется исправить."
        )
    if not due_at:
        raise ClosedDiscrepancyReviewError("Укажите срок исправления.")
    if timezone.is_naive(due_at):
        due_at = timezone.make_aware(
            due_at,
            timezone.get_current_timezone(),
        )
    if due_at <= timezone.now():
        raise ClosedDiscrepancyReviewError(
            "Срок исправления должен быть в будущем."
        )
    try:
        normalized_assignee_id = int(assignee_id)
    except (TypeError, ValueError):
        normalized_assignee_id = 0
    if not normalized_assignee_id:
        raise ClosedDiscrepancyReviewError(
            "Выберите исполнителя участка обработки."
        )

    with transaction.atomic():
        entries = list(
            OrderAuditEntry.objects.select_for_update(of=("self",))
            .filter(order_type="processing", order_id=normalized_order_id)
            .select_related("agency")
            .order_by("created_at", "id")
        )
        if not entries:
            raise ClosedDiscrepancyReviewError(
                "Заявка на обработку не найдена."
            )
        report = audit_closed_processing_discrepancies(
            order_ids=[normalized_order_id],
            sample_limit=1,
        )
        if not report.rows:
            raise ClosedDiscrepancyReviewError(
                "Заявка не закрыта или расхождение количеств отсутствует."
            )
        current_review = latest_closed_discrepancy_review(entries)
        if (
            str(current_review.get("status") or "").strip()
            != REVIEW_STATUS_CORRECTION_REQUIRED
        ):
            raise ClosedDiscrepancyReviewError(
                "Сначала зафиксируйте решение «Требуется отдельная "
                "корректировка»."
            )

        assignee = Employee.objects.filter(
            pk=normalized_assignee_id,
            role__in=CORRECTION_TASK_ASSIGNEE_ROLES,
            is_active=True,
        ).first()
        if not assignee:
            raise ClosedDiscrepancyReviewError(
                "Исполнитель должен быть активным сотрудником обработки."
            )

        discrepancy = report.rows[0]
        route = closed_discrepancy_correction_task_route(normalized_order_id)
        title = closed_discrepancy_correction_task_title(normalized_order_id)
        description = (
            f"Заявка на обработку №{normalized_order_id}. "
            f"Заявлено: {discrepancy.declared_qty}; "
            f"обработано: {discrepancy.processed_qty}; "
            f"в коробах: {discrepancy.boxed_qty}.\n"
            f"Что исправить: {normalized_comment}"
        )
        task = (
            Task.objects.select_for_update()
            .filter(route=route, title=title)
            .order_by("-updated_at", "-id")
            .first()
        )
        created = task is None
        updated = False
        if task is None:
            task = Task.objects.create(
                title=title,
                description=description,
                route=route,
                assigned_to=assignee,
                observer=get_employee_for_user(user),
                created_by=(
                    user
                    if getattr(user, "is_authenticated", False)
                    else None
                ),
                status="in_progress",
                priority="high",
                due_date=due_at,
            )
        else:
            changed_fields = []
            expected_values = {
                "description": description,
                "assigned_to": assignee,
                "observer": get_employee_for_user(user),
                "status": "in_progress",
                "priority": "high",
                "due_date": due_at,
            }
            for field_name, expected_value in expected_values.items():
                if getattr(task, field_name) != expected_value:
                    setattr(task, field_name, expected_value)
                    changed_fields.append(field_name)
            if changed_fields:
                task.save(update_fields=[*changed_fields, "updated_at"])
                updated = True

        if created or updated:
            task_payload = {
                "task_id": task.pk,
                "task_state": "created" if created else "updated",
                "task_status": task.status,
                "task_priority": task.priority,
                "assignee_id": assignee.pk,
                "assignee_name": assignee.full_name,
                "assignee_role": assignee.role,
                "due_at": timezone.localtime(due_at).isoformat(),
                "comment": normalized_comment,
                "source": "processing_head",
                "quantity_snapshot": {
                    "declared_qty": discrepancy.declared_qty,
                    "processed_qty": discrepancy.processed_qty,
                    "boxed_qty": discrepancy.boxed_qty,
                },
            }
            OrderAuditEntry.objects.create(
                order_id=normalized_order_id,
                order_type="processing",
                action="update",
                user=(
                    user
                    if getattr(user, "is_authenticated", False)
                    else None
                ),
                agency=entries[-1].agency,
                description=(
                    "Руководитель обработки "
                    f"{'создал' if created else 'обновил'} задачу "
                    f"корректировки №{task.pk}"
                ),
                payload={
                    "processing_discrepancy_correction_task": task_payload,
                    "internal_only": True,
                },
            )

        return ClosedDiscrepancyCorrectionTaskResult(
            order_id=normalized_order_id,
            task_id=task.pk,
            assignee_name=assignee.full_name,
            due_at=task.due_date,
            created=created,
            updated=updated,
        )
