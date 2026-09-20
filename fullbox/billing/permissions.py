from __future__ import annotations

from django.db.models import Q

from employees.access import get_employee_roles, get_request_employee


POWER_ROLES = {"head_manager", "director", "admin"}
ACCOUNTING_ROLES = {"accountant", "director", "admin"}
VIEW_ROLES = {"manager", "head_manager", "accountant", "admin", "director"}


def get_employee(user_or_request):
    if hasattr(user_or_request, "user"):
        return get_request_employee(user_or_request)
    user = user_or_request
    if not user or not getattr(user, "is_authenticated", False):
        return None
    return getattr(user, "employee_profile", None)


def get_billing_role(user_or_request) -> str | None:
    employee = get_employee(user_or_request)
    if employee:
        roles = get_employee_roles(employee)
        for role in ("admin", "director", "accountant", "head_manager"):
            if role in roles:
                return role
        return employee.role
    user = getattr(user_or_request, "user", user_or_request)
    if user and getattr(user, "is_superuser", False):
        return "admin"
    if hasattr(user_or_request, "user"):
        from employees.access import get_request_role

        return get_request_role(user_or_request)
    return None


def _client_is_managers(client, employee) -> bool:
    if not client or not employee:
        return False
    return str(getattr(client, "mened_user_id", "") or "") == str(getattr(employee, "user_id", "") or "")


def filter_agencies_for_user(queryset, user_or_request):
    """Портфель клиентов для менеджера: закреплённые + без менеджера (mened_user_id пуст)."""
    role = get_billing_role(user_or_request)
    if role in POWER_ROLES or role == "accountant":
        return queryset
    queryset = queryset.filter(archived=False, lifecycle__status="active")
    if role == "manager":
        employee = get_employee(user_or_request)
        if not employee or not employee.user_id:
            return queryset.none()
        # Пока закрепление не заполнено — дни хранения/биллинг иначе пустые у всех менеджеров.
        return queryset.filter(Q(mened_user_id=employee.user_id) | Q(mened_user_id__isnull=True))
    employee = get_employee(user_or_request)
    if not employee or not employee.user_id:
        return queryset.none()
    return queryset.filter(Q(mened_user_id=employee.user_id) | Q(mened_user_id__isnull=True))


def _client_unassigned(client) -> bool:
    raw = getattr(client, "mened_user_id", None)
    return raw in (None, "", 0)


def can_view_billing(user_or_request, application=None) -> bool:
    role = get_billing_role(user_or_request)
    if role not in VIEW_ROLES:
        return False
    if role in POWER_ROLES or role == "accountant":
        return True
    employee = get_employee(user_or_request)
    if role == "manager":
        if not employee:
            return False
        if application is None:
            return True
        lifecycle = getattr(application.client, "lifecycle", None)
        if lifecycle is None:
            try:
                lifecycle = application.client.lifecycle
            except Exception:
                lifecycle = None
        if (
            getattr(application.client, "archived", False)
            or not lifecycle
            or getattr(lifecycle, "status", "") != "active"
        ):
            return False
        if application.manager_id == employee.id or _client_is_managers(application.client, employee):
            return True
        # Клиент без закреплённого менеджера — биллинг доступен любому менеджеру
        return _client_unassigned(application.client)
    if application is None:
        return bool(employee)
    if not employee:
        return False
    return application.manager_id == employee.id or _client_is_managers(application.client, employee)


def can_manage_charges(user_or_request, application=None) -> bool:
    """Оперативная работа с открытыми начислениями менеджером и бухгалтером."""
    role = get_billing_role(user_or_request)
    if role in POWER_ROLES or role == "accountant":
        return True
    employee = get_employee(user_or_request)
    if not employee:
        return False
    return employee.role == "manager" and (application is None or can_view_billing(user_or_request, application))


def can_generate_act(user_or_request, application=None) -> bool:
    return can_manage_charges(user_or_request, application)


def can_create_invoice(user_or_request, application=None) -> bool:
    """Проведение/выставление счёта — бухгалтер и power-роли."""
    role = get_billing_role(user_or_request)
    if role in ACCOUNTING_ROLES or role in POWER_ROLES:
        return True
    return False


def can_create_invoice_draft(user_or_request, application=None) -> bool:
    """Черновик счёта может создать менеджер (своего клиента) или бухгалтер."""
    if can_create_invoice(user_or_request, application):
        return True
    role = get_billing_role(user_or_request)
    if role == "manager":
        return application is None or can_view_billing(user_or_request, application)
    return False


def can_submit_document_review(user_or_request, application=None) -> bool:
    """Передать черновик бухгалтеру — менеджер портфеля или бухгалтер."""
    role = get_billing_role(user_or_request)
    if role in ACCOUNTING_ROLES or role in POWER_ROLES:
        return True
    if role == "manager":
        return application is None or can_view_billing(user_or_request, application)
    return False


def can_review_billing_document(user_or_request) -> bool:
    """Принять/вернуть документ — только бухгалтер / power."""
    role = get_billing_role(user_or_request)
    return role in ACCOUNTING_ROLES or role in POWER_ROLES


def can_register_payment(user_or_request, invoice=None) -> bool:
    role = get_billing_role(user_or_request)
    return role in ACCOUNTING_ROLES


def can_financially_close(user_or_request, application=None) -> bool:
    return can_register_payment(user_or_request)


def can_mark_billing_external(user_or_request, application=None) -> bool:
    """Пометить «счета вне системы / не выставлять» — бухгалтер и power-роли."""
    role = get_billing_role(user_or_request)
    return role in POWER_ROLES or role == "accountant"


def can_view_client_tariff(user_or_request, client=None) -> bool:
    role = get_billing_role(user_or_request)
    if role in POWER_ROLES or role in {"accountant", "director"}:
        return True
    employee = get_employee(user_or_request)
    if role == "manager" and employee:
        return client is None or _client_is_managers(client, employee)
    return False


def can_add_client_tariff(user_or_request, client=None) -> bool:
    """Тарифы заводит/редактирует бухгалтер (и power-роли), не менеджер."""
    role = get_billing_role(user_or_request)
    return role in POWER_ROLES or role == "accountant"


def can_change_client_tariff_draft(user_or_request, client=None) -> bool:
    return can_add_client_tariff(user_or_request, client)


def can_approve_client_tariff(user_or_request, client=None) -> bool:
    role = get_billing_role(user_or_request)
    return role in POWER_ROLES or role in {"accountant", "director"}


def can_archive_client_tariff(user_or_request, client=None) -> bool:
    return can_approve_client_tariff(user_or_request, client)


def can_export_client_tariff(user_or_request, client=None) -> bool:
    return can_view_client_tariff(user_or_request, client)


def can_override_billing_tariff(user_or_request) -> bool:
    """Свободную цену в начислении задаёт главный менеджер или руководитель."""
    return get_billing_role(user_or_request) in POWER_ROLES


def can_propose_client_tariff_price(user_or_request, client=None) -> bool:
    """Главный менеджер создаёт внутреннюю редакцию цены клиента на согласование."""
    return get_billing_role(user_or_request) in POWER_ROLES


def can_edit_invoice_prices(user_or_request, invoice=None) -> bool:
    """Право бухгалтерской роли менять цену в карточке чернового счёта."""
    user = getattr(user_or_request, "user", user_or_request)
    employee = get_employee(user_or_request)
    if not employee or not getattr(user, "is_active", False):
        return False
    return get_billing_role(user_or_request) in ACCOUNTING_ROLES | POWER_ROLES


def can_delete_charge(user_or_request, application=None) -> bool:
    """Удаление начислений — бухгалтер/power; менеджер не удаляет строки WMS."""
    role = get_billing_role(user_or_request)
    if role in POWER_ROLES or role == "accountant":
        return application is None or can_view_billing(user_or_request, application)
    return False


def can_change_charge_vat(user_or_request) -> bool:
    """НДС в начислении меняет только бухгалтер/power."""
    return can_override_billing_tariff(user_or_request)


def filter_applications_for_user(queryset, user_or_request):
    role = get_billing_role(user_or_request)
    if role in POWER_ROLES or role == "accountant":
        return queryset
    if role == "manager":
        employee = get_employee(user_or_request)
        if not employee or not employee.user_id:
            return queryset.none()
        # Закреплённые клиенты + заявки без менеджера клиента (иначе биллинг «пропадает»)
        return queryset.filter(client__archived=False).filter(
            Q(client__mened_user_id=employee.user_id)
            | Q(manager=employee)
            | Q(client__mened_user_id__isnull=True)
        ).filter(Q(client__lifecycle__status="active") | Q(client__lifecycle__isnull=True))
    employee = get_employee(user_or_request)
    if not employee or employee.role not in VIEW_ROLES:
        return queryset.none()
    user_id = getattr(employee, "user_id", None)
    return queryset.filter(Q(manager=employee) | Q(client__mened_user_id=user_id) | Q(client__mened_user_id__isnull=True))
