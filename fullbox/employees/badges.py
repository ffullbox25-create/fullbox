from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.signing import salted_hmac
from django.db import transaction
from django.utils import timezone

from .models import Employee, EmployeeBadge, EmployeeBadgeEvent


BADGE_PREFIX = "FBX-EMP-"
BADGE_TOKEN_HEX_BYTES = 24
WAREHOUSE_QR_ROLES = frozenset(
    {
        "picker",
        "storekeeper",
        "fbs_controller",
        "packer",
        "processing_head",
        "processing_worker",
        "reachtruck_driver",
        "driver",
    }
)


def token_lookup(raw_token: str) -> str:
    return salted_hmac("employees.qr_badge.lookup", raw_token).hexdigest()


def active_badge_for_employee(employee: Employee) -> EmployeeBadge | None:
    return (
        EmployeeBadge.objects.select_related("issued_by")
        .filter(employee=employee, revoked_at__isnull=True)
        .order_by("-issued_at", "-id")
        .first()
    )


def find_active_badge(raw_token: str) -> EmployeeBadge | None:
    raw_token = str(raw_token or "").strip()
    candidates = (raw_token, raw_token.upper())
    for candidate in dict.fromkeys(candidates):
        if (
            not candidate.startswith(BADGE_PREFIX)
            or len(candidate) < len(BADGE_PREFIX) + 24
        ):
            continue
        badge = (
            EmployeeBadge.objects.select_related("employee", "employee__user")
            .filter(token_lookup=token_lookup(candidate), revoked_at__isnull=True)
            .order_by("-issued_at", "-id")
            .first()
        )
        if badge is not None and check_password(candidate, badge.token_digest):
            return badge
    return None


def ensure_qr_login_user(*, employee: Employee):
    """Create a locked-down technical account used only by QR login."""
    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    if not employee.is_active:
        raise ValueError("Нельзя выдать QR-бейдж неактивному сотруднику.")
    if employee.user_id:
        User = get_user_model()
        user = User.objects.select_for_update().get(pk=employee.user_id)
        if not user.is_active:
            raise ValueError("Учётная запись сотрудника отключена.")
        return user

    User = get_user_model()
    username_base = f"{employee.role or 'warehouse'}_qr_{employee.pk}"
    username = username_base
    suffix = 1
    while User.objects.filter(username=username).exists():
        suffix += 1
        username = f"{username_base}_{suffix}"
    user = User(
        username=username,
        email=employee.email or "",
        is_active=True,
    )
    user.set_unusable_password()
    user.save()
    employee.user = user
    employee.save(update_fields=("user", "updated_at"))
    return user


@transaction.atomic
def issue_badge(*, employee: Employee, actor) -> tuple[EmployeeBadge, str]:
    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    if employee.role not in WAREHOUSE_QR_ROLES:
        raise ValueError("Вход по QR разрешён только для складских ролей.")
    ensure_qr_login_user(employee=employee)
    employee.refresh_from_db()
    now = timezone.now()
    active_badges = list(
        EmployeeBadge.objects.select_for_update().filter(
            employee=employee,
            revoked_at__isnull=True,
        )
    )
    if active_badges:
        EmployeeBadge.objects.filter(pk__in=[badge.pk for badge in active_badges]).update(
            revoked_at=now,
            revoked_by=actor,
        )
        EmployeeBadgeEvent.objects.create(
            event_type=EmployeeBadgeEvent.EVENT_REVOKED,
            employee=employee,
            actor=actor,
            details={"reason": "reissued"},
        )

    if employee.qr_login_enabled:
        employee.qr_login_enabled = False
        employee.save(update_fields=("qr_login_enabled", "updated_at"))
        EmployeeBadgeEvent.objects.create(
            event_type=EmployeeBadgeEvent.EVENT_ACCESS_DISABLED,
            employee=employee,
            actor=actor,
            details={"reason": "badge_reissued"},
        )

    raw_token = f"{BADGE_PREFIX}{secrets.token_hex(BADGE_TOKEN_HEX_BYTES).upper()}"
    badge = EmployeeBadge.objects.create(
        employee=employee,
        token_lookup=token_lookup(raw_token),
        token_digest=make_password(raw_token),
        issued_by=actor,
    )
    EmployeeBadgeEvent.objects.create(
        event_type=EmployeeBadgeEvent.EVENT_ISSUED,
        employee=employee,
        actor=actor,
    )
    if not employee.qr_login_enabled:
        employee.qr_login_enabled = True
        employee.save(update_fields=("qr_login_enabled", "updated_at"))
        EmployeeBadgeEvent.objects.create(
            event_type=EmployeeBadgeEvent.EVENT_ACCESS_ENABLED,
            employee=employee,
            actor=actor,
            details={"reason": "badge_issued"},
        )
    return badge, raw_token


@transaction.atomic
def set_qr_login_enabled(*, employee: Employee, enabled: bool, actor) -> Employee:
    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    if enabled:
        if not EmployeeBadge.objects.filter(
            employee=employee,
            revoked_at__isnull=True,
        ).exists():
            raise ValueError("Сначала выдайте сотруднику QR-бейдж.")
        if employee.role not in WAREHOUSE_QR_ROLES:
            raise ValueError("Вход по QR разрешён только для складских ролей.")
        if not employee.is_active or not employee.user_id or not employee.user.is_active:
            raise ValueError("Для входа нужна активная учётная запись сотрудника.")

    if employee.qr_login_enabled == enabled:
        return employee
    employee.qr_login_enabled = enabled
    employee.save(update_fields=("qr_login_enabled", "updated_at"))
    EmployeeBadgeEvent.objects.create(
        event_type=(
            EmployeeBadgeEvent.EVENT_ACCESS_ENABLED
            if enabled
            else EmployeeBadgeEvent.EVENT_ACCESS_DISABLED
        ),
        employee=employee,
        actor=actor,
        details={"reason": "manual"},
    )
    return employee


@transaction.atomic
def revoke_badge(*, employee: Employee, actor) -> None:
    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    now = timezone.now()
    active_badges = list(
        EmployeeBadge.objects.select_for_update().filter(
            employee=employee,
            revoked_at__isnull=True,
        )
    )
    if active_badges:
        EmployeeBadge.objects.filter(pk__in=[badge.pk for badge in active_badges]).update(
            revoked_at=now,
            revoked_by=actor,
        )
        EmployeeBadgeEvent.objects.create(
            event_type=EmployeeBadgeEvent.EVENT_REVOKED,
            employee=employee,
            actor=actor,
            details={"reason": "manual"},
        )
    if employee.qr_login_enabled:
        employee.qr_login_enabled = False
        employee.save(update_fields=("qr_login_enabled", "updated_at"))
        EmployeeBadgeEvent.objects.create(
            event_type=EmployeeBadgeEvent.EVENT_ACCESS_DISABLED,
            employee=employee,
            actor=actor,
            details={"reason": "badge_revoked"},
        )
