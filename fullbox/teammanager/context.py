"""Контекст единого кабинета менеджера (роль, меню)."""


def team_cabinet(request):
    path = getattr(request, "path", "") or ""
    in_team = path.startswith("/team-manager")
    in_logistics = path.startswith("/logistics")
    if not in_team and not in_logistics:
        return {}
    from employees.access import get_request_effective_role, get_request_roles

    from .roles import CABINET_ROLES, can_see_billing_nav, role_label

    access_roles = get_request_roles(request)
    role = get_request_effective_role(
        request,
        preferred_roles=("admin", "director", "head_manager", "manager", "logistician"),
    )
    if role not in CABINET_ROLES:
        # Кладовщик и др. на /logistics/ — минимальный контекст оболочки
        return {
            "cabinet_role": role,
            "cabinet_role_label": role_label(role) if role else "Сотрудник",
            "show_billing_nav": False,
            "show_trips_nav": in_logistics,
            "use_cabinet_shell": False,
            "show_head_manager_nav": "head_manager" in access_roles,
            "show_accountant_cabinet_nav": "accountant" in access_roles,
        }
    return {
        "cabinet_role": role,
        "cabinet_role_label": role_label(role),
        "show_billing_nav": can_see_billing_nav(role),
        "show_trips_nav": True,
        "use_cabinet_shell": True,
        "show_head_manager_nav": "head_manager" in access_roles,
        "show_accountant_cabinet_nav": "accountant" in access_roles,
    }
