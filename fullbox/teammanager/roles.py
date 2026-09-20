"""Роли единого кабинета менеджера FULLBOX (менеджер + логист + руководители)."""

from __future__ import annotations

# Один интерфейс — различия только правами и фильтрами.
CABINET_ROLES = (
    "manager",
    "logistician",
    "head_manager",
    "director",
    "admin",
    "developer",
)

# Финансовый раздел в меню — не для логиста по умолчанию.
BILLING_NAV_ROLES = (
    "manager",
    "head_manager",
    "director",
    "admin",
    "developer",
)

ROLE_LABELS = {
    "manager": "Клиентский менеджер",
    "logistician": "Логист",
    "head_manager": "Старший менеджер",
    "director": "Руководитель",
    "admin": "Администратор",
    "developer": "Разработчик",
}

# scope=… для единого реестра задач
SCOPE_CHOICES = (
    ("mine", "Мои задачи"),
    ("department", "Задачи моего подразделения"),
    ("all", "Все доступные"),
    ("unassigned", "Без ответственного"),
    ("role_manager", "Клиентский менеджер"),
    ("role_logistician", "Логист"),
    ("lane_billing", "Биллинг"),
    ("lane_warehouse", "Склад"),
    ("needs_reassign", "Требуют замены сотрудника"),
)

LANE_CHOICES = (
    ("all", "Все типы"),
    ("ops_logistics", "Отгрузки и рейсы"),
)


def role_label(role: str | None) -> str:
    if not role:
        return "Сотрудник"
    return ROLE_LABELS.get(role, role)


def can_see_billing_nav(role: str | None) -> bool:
    return role in BILLING_NAV_ROLES


def default_scope(role: str | None) -> str:
    if role == "logistician":
        return "mine"
    if role in {"head_manager", "director", "admin", "developer"}:
        return "all"
    return "all"


def default_lane(role: str | None) -> str:
    if role == "logistician":
        return "ops_logistics"
    return "all"


def default_type_tab(role: str | None) -> str:
    """Явный type= в URL имеет приоритет; иначе вкладка «Все» / логистика."""
    if role == "logistician":
        return "all"
    return "all"
