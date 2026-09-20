"""Helpers for client portal employees (AgencyPortalMember)."""
from __future__ import annotations

import secrets
import string
from functools import wraps
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import JsonResponse

from .models import AgencyPortalMember
from .portal_access import (
    ROLE_ADMIN,
    ROLE_CREATION_CHOICES,
    SECTION_CHOICES,
    SECTION_KEYS,
    initials_from_name,
    role_label,
    role_legend,
    sections_for_role,
    sections_summary,
)


def _generate_temp_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _build_member_username(*, agency_id: int, email: str) -> str:
    User = get_user_model()
    local = str(email or "").split("@")[0].strip().lower() or "member"
    local = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in local)[:40]
    base = f"a{agency_id}_{local}"
    username = base
    counter = 1
    while User.objects.filter(username=username).exists():
        counter += 1
        username = f"{base}{counter}"
    return username


def get_portal_membership(user) -> AgencyPortalMember | None:
    if not user or not getattr(user, "is_authenticated", False):
        return None
    return (
        AgencyPortalMember.objects.select_related("agency", "user")
        .filter(user=user, is_active=True, agency__archived=False)
        .order_by("id")
        .first()
    )


def resolve_portal_agency_for_user(user, *, required_section: str | None = None):
    """Return Agency for portal owner or active portal member."""
    from sku.models import Agency

    if not user or not getattr(user, "is_authenticated", False):
        return None
    direct = Agency.objects.filter(portal_user=user, archived=False).first()
    if direct:
        return direct
    member = get_portal_membership(user)
    if member and required_section and member.role != ROLE_ADMIN:
        if required_section not in set(member.sections or []):
            return None
    return member.agency if member else None


def portal_user_has_section(user, agency, section: str) -> bool:
    if not user or not agency or not getattr(user, "is_authenticated", False):
        return False
    if getattr(agency, "portal_user_id", None) == getattr(user, "id", None):
        return True
    member = (
        AgencyPortalMember.objects.filter(agency=agency, user=user, is_active=True)
        .only("role", "sections")
        .order_by("id")
        .first()
    )
    if not member:
        return False
    return member.role == ROLE_ADMIN or str(section or "").strip() in set(member.sections or [])


def portal_user_is_admin(user, agency) -> bool:
    if not user or not agency or not getattr(user, "is_authenticated", False):
        return False
    if getattr(agency, "portal_user_id", None) == getattr(user, "id", None):
        return True
    return AgencyPortalMember.objects.filter(
        agency=agency,
        user=user,
        is_active=True,
        role=ROLE_ADMIN,
    ).exists()


def portal_section_required(section: str):
    """Protect an existing client API view without changing its URL contract."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            agency = resolve_portal_agency_for_user(getattr(request, "user", None))
            if agency is not None and not portal_user_has_section(request.user, agency, section):
                return JsonResponse({"ok": False, "error": "Недостаточно прав для раздела."}, status=403)
            if agency is None:
                from employees.access import get_request_role, is_staff_role

                user = getattr(request, "user", None)
                role = get_request_role(request) if user and getattr(user, "is_authenticated", False) else None
                if not user or (
                    not getattr(user, "is_staff", False) and not is_staff_role(role)
                ):
                    return JsonResponse({"ok": False, "error": "Доступ запрещен."}, status=403)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


def portal_sections_for_user(user, agency) -> list[str] | None:
    """
    None = full access (owner/staff path).
    list = restricted member sections.
    """
    if not user or not agency:
        return None
    if getattr(agency, "portal_user_id", None) and agency.portal_user_id == user.id:
        return list(SECTION_KEYS)
    member = (
        AgencyPortalMember.objects.filter(agency=agency, user=user, is_active=True)
        .order_by("id")
        .first()
    )
    if not member:
        return None
    if member.role == ROLE_ADMIN:
        return list(SECTION_KEYS)
    return list(member.sections or [])


def build_owner_employee_row(agency) -> dict[str, Any] | None:
    portal_user = getattr(agency, "portal_user", None)
    if not portal_user:
        return None
    fio = str(getattr(agency, "fio_agn", "") or "").strip()
    name_parts = [p for p in fio.replace(",", " ").split() if p]
    last_name = name_parts[0] if name_parts else (portal_user.last_name or portal_user.username or "Владелец")
    first_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else (portal_user.first_name or "")
    full_name = fio or f"{last_name} {first_name}".strip() or portal_user.username
    email = (agency.email or portal_user.email or "").strip() or "—"
    return {
        "id": None,
        "is_owner": True,
        "full_name": full_name,
        "email": email,
        "position": "Владелец кабинета",
        "role": ROLE_ADMIN,
        "role_label": role_label(ROLE_ADMIN),
        "sections": list(SECTION_KEYS),
        "sections_label": "Все разделы",
        "is_active": True,
        "initials": initials_from_name(last_name, first_name, full_name),
        "can_manage": False,
    }


def serialize_member(member: AgencyPortalMember) -> dict[str, Any]:
    sections = list(member.sections or [])
    return {
        "id": member.id,
        "is_owner": False,
        "full_name": member.full_name,
        "email": member.email,
        "position": member.position or "—",
        "role": member.role,
        "role_label": role_label(member.role),
        "sections": sections,
        "sections_label": sections_summary(sections),
        "is_active": bool(member.is_active),
        "initials": member.initials,
        "can_manage": True,
    }


def build_employees_context(*, agency) -> dict[str, Any]:
    owner = build_owner_employee_row(agency)
    members = [
        serialize_member(row)
        for row in AgencyPortalMember.objects.filter(agency=agency).order_by("last_name", "first_name", "id")
    ]
    employees = ([owner] if owner else []) + members
    return {
        "portal_employees": employees,
        "portal_role_choices": ROLE_CREATION_CHOICES,
        "portal_section_choices": SECTION_CHOICES,
        "portal_role_legend": role_legend(),
        "portal_section_keys": SECTION_KEYS,
    }


@transaction.atomic
def create_portal_employee(
    *,
    agency,
    last_name: str,
    first_name: str,
    email: str,
    position: str,
    role: str,
    custom_sections: list[str] | None,
    created_by=None,
) -> tuple[AgencyPortalMember, str]:
    User = get_user_model()
    email_norm = str(email or "").strip().lower()
    last_name = str(last_name or "").strip()
    first_name = str(first_name or "").strip()
    position = str(position or "").strip()
    role = str(role or "").strip().lower()
    if not last_name or not first_name or not email_norm:
        raise ValueError("Укажите фамилию, имя и email.")
    if role not in dict(ROLE_CREATION_CHOICES):
        raise ValueError("Выберите роль.")
    if AgencyPortalMember.objects.filter(agency=agency, email__iexact=email_norm).exists():
        raise ValueError("Сотрудник с таким email уже добавлен.")
    if agency.portal_user_id:
        owner = getattr(agency, "portal_user", None)
        if owner and str(owner.email or "").strip().lower() == email_norm:
            raise ValueError("Этот email уже используется владельцем кабинета.")

    sections = sections_for_role(role, custom_sections)
    if role != "custom" and not sections:
        sections = sections_for_role(role)
    if role == "custom" and not sections:
        raise ValueError("Для «Свой доступ» выберите хотя бы один раздел.")

    temp_password = _generate_temp_password()
    username = _build_member_username(agency_id=agency.id, email=email_norm)
    # A portal employee account belongs to one client cabinet. The same person
    # may work for several clients, but each cabinet must issue its own login
    # and password so requests and permissions cannot leak between agencies.
    user = User.objects.create_user(
        username=username,
        email=email_norm,
        password=temp_password,
        first_name=first_name,
        last_name=last_name,
    )

    member = AgencyPortalMember.objects.create(
        agency=agency,
        user=user,
        last_name=last_name,
        first_name=first_name,
        email=email_norm,
        position=position,
        role=role,
        sections=sections,
        is_active=True,
        created_by=created_by if getattr(created_by, "is_authenticated", False) else None,
    )
    return member, temp_password


def set_member_active(*, agency, member_id: int, is_active: bool) -> AgencyPortalMember:
    member = AgencyPortalMember.objects.get(pk=member_id, agency=agency)
    member.is_active = bool(is_active)
    member.save(update_fields=["is_active"])
    if member.user_id:
        user = member.user
        # Do not disable user if they own another agency.
        from sku.models import Agency

        if not Agency.objects.filter(portal_user_id=user.id).exists():
            user.is_active = bool(is_active)
            user.save(update_fields=["is_active"])
    return member


def delete_member(*, agency, member_id: int) -> None:
    member = AgencyPortalMember.objects.select_related("user").get(pk=member_id, agency=agency)
    user = member.user
    member.delete()
    if user is not None:
        from sku.models import Agency

        still_member = AgencyPortalMember.objects.filter(user=user).exists()
        owns = Agency.objects.filter(portal_user=user).exists()
        if not still_member and not owns:
            user.is_active = False
            user.save(update_fields=["is_active"])
