"""Portal employee roles and section access for client LK."""
from __future__ import annotations

from typing import Any

SECTION_REQUESTS = "requests"
SECTION_STOCK = "stock"
SECTION_FINANCE = "finance"
SECTION_REPORTS = "reports"
SECTION_NOTIFICATIONS = "notifications"
SECTION_PROFILE = "profile"

SECTION_CHOICES = (
    (SECTION_REQUESTS, "Заявки и отгрузки"),
    (SECTION_STOCK, "Остатки и товар"),
    (SECTION_FINANCE, "Финансы и документы"),
    (SECTION_REPORTS, "Отчёты и аналитика"),
    (SECTION_NOTIFICATIONS, "Уведомления и сообщения"),
    (SECTION_PROFILE, "Настройки профиля"),
)

SECTION_KEYS = [key for key, _label in SECTION_CHOICES]

ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_ACCOUNTANT = "accountant"
ROLE_OPERATOR = "operator"
ROLE_CUSTOM = "custom"

ROLE_CHOICES = (
    (ROLE_ADMIN, "Администратор"),
    (ROLE_MANAGER, "Менеджер"),
    (ROLE_ACCOUNTANT, "Бухгалтер"),
    (ROLE_OPERATOR, "Оператор"),
    (ROLE_CUSTOM, "Свой доступ"),
)

# «Оператор» оставлен в полном справочнике только для совместимости уже
# созданных сотрудников, но больше не предлагается и не принимается при
# создании новых сотрудников ЛК клиента.
ROLE_CREATION_CHOICES = tuple(
    choice for choice in ROLE_CHOICES if choice[0] != ROLE_OPERATOR
)

ROLE_PRESETS: dict[str, list[str]] = {
    ROLE_ADMIN: list(SECTION_KEYS),
    ROLE_MANAGER: [
        SECTION_REQUESTS,
        SECTION_STOCK,
        SECTION_REPORTS,
        SECTION_NOTIFICATIONS,
    ],
    ROLE_ACCOUNTANT: [
        SECTION_FINANCE,
        SECTION_REPORTS,
    ],
    ROLE_OPERATOR: [
        SECTION_REQUESTS,
    ],
    ROLE_CUSTOM: [],
}

ROLE_DESCRIPTIONS: dict[str, str] = {
    ROLE_ADMIN: "Полный доступ ко всем разделам и настройкам.",
    ROLE_MANAGER: "Доступ к заявкам, остаткам, отчётам и сообщениям.",
    ROLE_ACCOUNTANT: "Доступ к финансам, документам и отчётам.",
    ROLE_OPERATOR: "Доступ только к заявкам и статусам отгрузок.",
    ROLE_CUSTOM: "Выбор конкретных разделов вручную.",
}

# Map LK hash routes / profile link to section keys.
ROUTE_SECTION_MAP: dict[str, str] = {
    "/requests": SECTION_REQUESTS,
    "/stock": SECTION_STOCK,
    "/nomenclature": SECTION_STOCK,
    "/marking": SECTION_STOCK,
    "/marketplaces": SECTION_STOCK,
    "/fbs": SECTION_STOCK,
    "/knowledge": SECTION_REPORTS,
    "/finance": SECTION_FINANCE,
    "/billing": SECTION_FINANCE,
    "/notifications": SECTION_NOTIFICATIONS,
    "/chat": SECTION_NOTIFICATIONS,
    "/profile": SECTION_PROFILE,
}

# Server-side access map for the client cabinet API.  Dashboard/me are shared
# shell endpoints and intentionally stay outside this map; feature endpoints
# must be tied to the same sections as their navigation entries.
API_URL_SECTION_MAP: dict[str, str] = {
    "client-api-dashboard-live": SECTION_STOCK,
    "client-api-dashboard-live-history": SECTION_NOTIFICATIONS,
    "client-api-stock-journal": SECTION_STOCK,
    "client-api-stock-journal-export": SECTION_STOCK,
    "client-api-nomenclature": SECTION_STOCK,
    "client-api-nomenclature-export": SECTION_STOCK,
    "client-api-nomenclature-upload": SECTION_STOCK,
    "client-api-nomenclature-commit": SECTION_STOCK,
    "client-api-nomenclature-delete": SECTION_STOCK,
    "client-api-marketplaces": SECTION_STOCK,
    "client-api-marketplace-sync": SECTION_STOCK,
    "client-api-marking": SECTION_STOCK,
    "client-api-other-request-create": SECTION_REQUESTS,
    "client-api-other-attachment": SECTION_REQUESTS,
    "client-api-requests": SECTION_REQUESTS,
    "client-api-request-detail": SECTION_REQUESTS,
    "client-api-request-detail-export": SECTION_REQUESTS,
    "client-api-shipping-marking-export": SECTION_REQUESTS,
    "client-api-shipping-return-act-doc": SECTION_REQUESTS,
    "client-api-finance": SECTION_FINANCE,
    "client-api-finance-document-file": SECTION_FINANCE,
    "client-api-billing": SECTION_FINANCE,
    "client-api-billing-storage": SECTION_FINANCE,
    "client-api-billing-tariffs": SECTION_FINANCE,
    "client-api-tariff-version-current": SECTION_FINANCE,
    "client-api-tariff-version-history": SECTION_FINANCE,
    "client-api-tariff-version-detail": SECTION_FINANCE,
    "client-api-tariff-version-download": SECTION_FINANCE,
    "client-api-my-tariffs": SECTION_FINANCE,
    "client-api-requisites": SECTION_PROFILE,
    "client-api-requisites-change-request": SECTION_PROFILE,
    "client-api-v1-requisites": SECTION_PROFILE,
    "client-api-v1-requisites-change-request": SECTION_PROFILE,
    "client-api-notifications": SECTION_NOTIFICATIONS,
    "client-api-chat-threads": SECTION_NOTIFICATIONS,
    "client-api-chat-messages": SECTION_NOTIFICATIONS,
    "client-api-chat-reaction": SECTION_NOTIFICATIONS,
    "client-api-chat-message-edit": SECTION_NOTIFICATIONS,
    "client-api-chat-message-delete": SECTION_NOTIFICATIONS,
    "client-api-chat-mention-candidates": SECTION_NOTIFICATIONS,
    "client-api-chat-prefs": SECTION_NOTIFICATIONS,
    "client-api-chat-mark-all-read": SECTION_NOTIFICATIONS,
    "client-api-chat-attachment": SECTION_NOTIFICATIONS,
}


def sections_for_role(role: str, custom_sections: list[str] | None = None) -> list[str]:
    role_key = str(role or "").strip().lower()
    if role_key == ROLE_CUSTOM:
        allowed = {str(item).strip() for item in (custom_sections or []) if str(item).strip()}
        return [key for key in SECTION_KEYS if key in allowed]
    return list(ROLE_PRESETS.get(role_key, []))


def role_label(role: str) -> str:
    role_key = str(role or "").strip().lower()
    return dict(ROLE_CHOICES).get(role_key, "Свой доступ")


def sections_summary(sections: list[str] | None) -> str:
    keys = [key for key in (sections or []) if key in SECTION_KEYS]
    if not keys:
        return "Нет разделов"
    if len(keys) >= len(SECTION_KEYS):
        return "Все разделы"
    n = len(keys)
    mod10 = n % 10
    mod100 = n % 100
    if mod10 == 1 and mod100 != 11:
        word = "раздел"
    elif 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
        word = "раздела"
    else:
        word = "разделов"
    return f"{n} {word}"


def initials_from_name(last_name: str = "", first_name: str = "", fallback: str = "") -> str:
    parts = [str(last_name or "").strip(), str(first_name or "").strip()]
    letters = "".join(part[0] for part in parts if part)
    if letters:
        return letters[:2].upper()
    text = str(fallback or "").strip()
    if not text:
        return "??"
    words = [w for w in text.replace(",", " ").split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return text[:2].upper()


def can_access_route(sections: list[str] | None, route: str) -> bool:
    keys = set(sections or [])
    if not keys:
        return False
    if set(SECTION_KEYS).issubset(keys):
        return True
    section = ROUTE_SECTION_MAP.get(str(route or "").strip())
    if not section:
        return True
    return section in keys


def section_for_api_url_name(url_name: str | None) -> str | None:
    return API_URL_SECTION_MAP.get(str(url_name or "").strip())


def role_legend() -> list[dict[str, Any]]:
    return [
        {
            "role": role,
            "label": label,
            "description": ROLE_DESCRIPTIONS.get(role, ""),
            "sections": list(ROLE_PRESETS.get(role, [])),
        }
        for role, label in ROLE_CREATION_CHOICES
    ]
