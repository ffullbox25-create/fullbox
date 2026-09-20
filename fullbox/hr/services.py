from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import HrPayrollAuditEvent


AUDITED_FIELDS = {
    "HrDepartment": ("name", "code", "is_active"),
    "HrEmployeeProfile": ("department_id", "position", "personnel_number"),
    "HrEmployeePayRate": (
        "effective_from",
        "effective_to",
        "hourly_rate",
        "monthly_salary",
        "note",
    ),
    "HrTimesheetEntry": (
        "work_date",
        "planned_minutes",
        "worked_minutes",
        "late_minutes",
        "internship_minutes",
        "source",
        "source_key",
        "note",
    ),
    "HrPayrollAdjustment": (
        "month",
        "kind",
        "amount",
        "status",
        "description",
        "approved_by_id",
    ),
}


def _json_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def model_snapshot(instance) -> dict:
    fields = AUDITED_FIELDS.get(instance.__class__.__name__, ())
    return {field: _json_value(getattr(instance, field, None)) for field in fields}


def record_payroll_audit(
    *,
    action,
    object_type,
    object_id="",
    actor=None,
    employee=None,
    month=None,
    changes=None,
):
    return HrPayrollAuditEvent.objects.create(
        action=action,
        object_type=object_type,
        object_id=str(object_id or ""),
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        employee=employee,
        month=month,
        changes=changes or {},
    )


@transaction.atomic
def save_payroll_object(instance, *, actor=None):
    model_name = instance.__class__.__name__
    if model_name not in AUDITED_FIELDS:
        raise ValueError(f"Модель {model_name} не поддерживает HR-аудит.")
    before = {}
    action = HrPayrollAuditEvent.ACTION_CREATE
    if instance.pk:
        stored = instance.__class__.objects.select_for_update().get(pk=instance.pk)
        before = model_snapshot(stored)
        action = HrPayrollAuditEvent.ACTION_UPDATE
    instance.full_clean()
    instance.save()
    after = model_snapshot(instance)
    changed = {
        key: {"before": before.get(key), "after": value}
        for key, value in after.items()
        if before.get(key) != value
    }
    if (
        model_name == "HrPayrollAdjustment"
        and before.get("status") != "approved"
        and after.get("status") == "approved"
    ):
        action = HrPayrollAuditEvent.ACTION_APPROVE
    audit_month = getattr(instance, "month", None)
    if audit_month is None:
        audit_date = (
            getattr(instance, "work_date", None)
            or getattr(instance, "effective_from", None)
            or timezone.localdate()
        )
        audit_month = audit_date.replace(day=1)
    record_payroll_audit(
        action=action,
        object_type=model_name,
        object_id=instance.pk,
        actor=actor,
        employee=getattr(instance, "employee", None),
        month=audit_month,
        changes=changed,
    )
    return instance


@transaction.atomic
def delete_payroll_object(instance, *, actor=None):
    model_name = instance.__class__.__name__
    if model_name not in AUDITED_FIELDS:
        raise ValueError(f"Модель {model_name} не поддерживает HR-аудит.")
    object_id = instance.pk
    employee = getattr(instance, "employee", None)
    audit_date = (
        getattr(instance, "month", None)
        or getattr(instance, "work_date", None)
        or getattr(instance, "effective_from", None)
        or timezone.localdate()
    )
    before = model_snapshot(instance)
    instance.delete()
    record_payroll_audit(
        action=HrPayrollAuditEvent.ACTION_DELETE,
        object_type=model_name,
        object_id=object_id,
        actor=actor,
        employee=employee,
        month=audit_date.replace(day=1),
        changes={"before": before},
    )
