"""Права секций кабинета бухгалтера на текущих ролях (без новых ролей в employees)."""
from __future__ import annotations

# Роли, которые уже есть в системе и допускаются в кабинет.
ACCOUNTANT_CABINET_ROLES = frozenset({"accountant", "admin", "director", "head_manager"})

# Пустой set = только accountant/admin/director.
_DEFAULT = frozenset({"accountant", "admin", "director"})
_WITH_HM = frozenset({"accountant", "admin", "director", "head_manager"})

MENU_ROLE_MAP: dict[str, frozenset[str]] = {
    "overview": _WITH_HM,
    "clients": _WITH_HM,
    "clients_all": _WITH_HM,
    "clients_legal": _WITH_HM,
    "clients_requisites": _WITH_HM,
    "clients_finance": _WITH_HM,
    "clients_archive": _WITH_HM,
    "contracts": _WITH_HM,
    "contracts_list": _WITH_HM,
    "contracts_kp": _WITH_HM,
    "contracts_addenda": _WITH_HM,
    "contracts_expiring": _WITH_HM,
    "contracts_signing": _WITH_HM,
    "contracts_archive": _WITH_HM,
    "tariffs": _WITH_HM,
    "tariffs_catalog": _WITH_HM,
    "tariffs_clients": _WITH_HM,
    "tariffs_drafts": _WITH_HM,
    "tariffs_review": _WITH_HM,
    "tariffs_approval": _WITH_HM,
    "tariffs_published": _WITH_HM,
    "tariffs_no_price": _WITH_HM,
    "tariffs_history": _WITH_HM,
    "tariffs_templates": _WITH_HM,
    "charges": _WITH_HM,
    "charges_all": _WITH_HM,
    "charges_apps": _WITH_HM,
    "charges_storage": _WITH_HM,
    "charges_materials": _WITH_HM,
    "charges_extra": _WITH_HM,
    "charges_manual": _WITH_HM,
    "charges_no_price": _WITH_HM,
    "charges_errors": _WITH_HM,
    "charges_adjustments": _WITH_HM,
    "billing": _WITH_HM,
    "billing_overview": _WITH_HM,
    "billing_apps": _WITH_HM,
    "billing_check": _WITH_HM,
    "billing_drafts": _WITH_HM,
    "billing_ready": _WITH_HM,
    "billing_errors": _WITH_HM,
    "billing_close": _WITH_HM,
    "documents": _WITH_HM,
    "documents_invoices": _WITH_HM,
    "documents_acts": _WITH_HM,
    "documents_upd": _DEFAULT,
    "documents_sf": _DEFAULT,
    "documents_corrections": _DEFAULT,
    "documents_reconcile": _DEFAULT,
    "documents_attachments": _WITH_HM,
    "documents_send": _WITH_HM,
    "documents_sign": _WITH_HM,
    "documents_archive": _WITH_HM,
    "payments": _WITH_HM,
    "payments_incoming": _WITH_HM,
    "payments_allocate": _DEFAULT,
    "payments_unidentified": _DEFAULT,
    "payments_partial": _WITH_HM,
    "payments_advances": _DEFAULT,
    "payments_ar": _WITH_HM,
    "payments_overdue": _WITH_HM,
    "payments_promises": _WITH_HM,
    "suppliers": _DEFAULT,
    "payment_requests": _DEFAULT,
    "payment_calendar": _DEFAULT,
    "banks": _DEFAULT,
    "periods": _DEFAULT,
    "reports": _WITH_HM,
    "legal_entities": _DEFAULT,
    "le_all": _DEFAULT,
    "carriers": _DEFAULT,
    "carriers_all": _DEFAULT,
    "carriers_new": _DEFAULT,
    "integrations": _DEFAULT,
    "settings": _DEFAULT,
    "audit": _DEFAULT,
    "audit_all": _WITH_HM,
    # legacy active_nav keys
    "history": _WITH_HM,
    "tariff_check": _WITH_HM,
    "storage": _WITH_HM,
    "own_companies": _DEFAULT,
}

# Префикс ключа секции → роли (для вложенных stub-путей)
_PREFIX_ROLES: tuple[tuple[str, frozenset[str]], ...] = (
    ("clients", _WITH_HM),
    ("contracts", _WITH_HM),
    ("tariffs", _WITH_HM),
    ("charges", _WITH_HM),
    ("billing", _WITH_HM),
    ("documents", _WITH_HM),
    ("payments", _WITH_HM),
    ("suppliers", _DEFAULT),
    ("payment_requests", _DEFAULT),
    ("payment_calendar", _DEFAULT),
    ("banks", _DEFAULT),
    ("periods", _DEFAULT),
    ("reports", _WITH_HM),
    ("legal_entities", _DEFAULT),
    ("carriers", _DEFAULT),
    ("integrations", _DEFAULT),
    ("settings", _DEFAULT),
    ("audit", _DEFAULT),
)


def can_see_menu_key(role: str | None, key: str) -> bool:
    if not role:
        return False
    if role in {"admin", "director"}:
        return True
    allowed = MENU_ROLE_MAP.get(key)
    if allowed is not None:
        return role in allowed
    for prefix, roles in _PREFIX_ROLES:
        if key == prefix or key.startswith(prefix + "_"):
            return role in roles
    return role in _DEFAULT
