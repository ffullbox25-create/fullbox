from django.conf import settings
from django.contrib.auth.mixins import AccessMixin
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseForbidden

from .models import Employee


STAFF_ROLES = {
    "admin",
    "director",
    "hr",
    "head_manager",
    "processing_head",
    "processing_worker",
    "manager",
    "storekeeper",
    "fbs_controller",
    "logistician",
    "driver",
    "accountant",
    "reachtruck_driver",
    "super_car",
    "developer",
}

_REQUEST_EMPLOYEE_CACHE_ATTR = "_fullbox_request_employee_cache"
_REQUEST_EMPLOYEE_CACHE_MISSING = object()


def is_staff_role(role: str | None) -> bool:
    return role in STAFF_ROLES


def has_developer_access(role: str | None) -> bool:
    return role == "developer"


def is_developer_login(user) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "username", "") == "dev"
    )


def role_can_access(role: str | None, roles) -> bool:
    return has_developer_access(role) or role in roles


def get_employee_roles(employee: Employee | None) -> frozenset[str]:
    if not employee or not employee.is_active:
        return frozenset()
    roles = {str(employee.role or "").strip()}
    roles.update(employee.normalized_access_roles())
    roles.discard("")
    return frozenset(roles)


def employee_has_role(employee: Employee | None, role: str) -> bool:
    return role in get_employee_roles(employee)


def get_employee_for_user(user, *, preferred_id: int | None = None, preferred_role: str | None = None):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    employees = Employee.objects.filter(user=user, is_active=True).order_by("id")
    if preferred_id:
        employee = employees.filter(id=preferred_id).first()
        if employee:
            return employee
    if preferred_role:
        employee = employees.filter(role=preferred_role).first()
        if employee:
            return employee
    return employees.first()


def get_request_employee(request):
    if not request or not getattr(request, "user", None):
        return None
    user = request.user
    if not getattr(user, "is_authenticated", False):
        return None

    preferred_id = None
    preferred_role = None
    session = getattr(request, "session", None)
    raw_id = session.get("employee_id") if session is not None else None
    raw_role = session.get("employee_role") if session is not None else None
    raw_impersonated_id = (
        session.get("developer_impersonated_employee_id")
        if session is not None
        else None
    )
    cache_key = (
        getattr(user, "pk", None),
        raw_id,
        raw_role,
        raw_impersonated_id,
    )
    cached = getattr(
        request,
        _REQUEST_EMPLOYEE_CACHE_ATTR,
        _REQUEST_EMPLOYEE_CACHE_MISSING,
    )
    if cached is not _REQUEST_EMPLOYEE_CACHE_MISSING and cached[0] == cache_key:
        return cached[1]

    employee = None
    if session is not None:
        if is_developer_login(user):
            try:
                impersonated_id = int(raw_impersonated_id)
            except (TypeError, ValueError):
                impersonated_id = None
            if impersonated_id:
                employee = Employee.objects.filter(id=impersonated_id, is_active=True).first()
        if employee is not None:
            setattr(request, _REQUEST_EMPLOYEE_CACHE_ATTR, (cache_key, employee))
            return employee
        try:
            preferred_id = int(raw_id)
        except (TypeError, ValueError):
            preferred_id = None
        preferred_role = str(raw_role or "").strip() or None
    employee = get_employee_for_user(
        user,
        preferred_id=preferred_id,
        preferred_role=preferred_role,
    )
    setattr(request, _REQUEST_EMPLOYEE_CACHE_ATTR, (cache_key, employee))
    return employee


def get_request_role(request):
    employee = get_request_employee(request)
    if employee:
        return employee.role
    if request.user.is_authenticated and request.user.username == "dev":
        return "developer"
    if settings.DEBUG:
        role = request.session.get("employee_role")
        if role:
            return role
        if request.user.is_authenticated:
            return request.user.username
    return None


def get_request_roles(request) -> frozenset[str]:
    employee = get_request_employee(request)
    if employee:
        return get_employee_roles(employee)
    role = get_request_role(request)
    return frozenset({role}) if role else frozenset()


def get_request_effective_role(request, preferred_roles=()) -> str | None:
    roles = get_request_roles(request)
    for role in preferred_roles:
        if role in roles:
            return role
    employee = get_request_employee(request)
    if employee:
        return employee.role
    return get_request_role(request)


def request_has_any_role(request, roles) -> bool:
    return bool(get_request_roles(request).intersection(set(roles)))


def role_required(*roles):
    def decorator(view_func):
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
            if not is_developer_login(request.user) and not request_has_any_role(request, roles):
                return HttpResponseForbidden("Доступ запрещен")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


class RoleRequiredMixin(AccessMixin):
    allowed_roles = ()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if (
            self.allowed_roles
            and not is_developer_login(request.user)
            and not request_has_any_role(request, self.allowed_roles)
        ):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)


def resolve_cabinet_url(role: str | None, *, client_id: int | None = None) -> str:
    mapping = {
        "manager": "/team-manager/",
        "accountant": "/accountant/",
        "hr": "/hr/",
        "storekeeper": "/sklad/",
        "fbs_controller": "/fbs/controller/",
        "logistician": "/team-manager/",
        "driver": "/logistics/driver/trips/",
        "head_manager": "/head-manager/",
        "processing_head": "/processing-head/",
        "packer": "/processing-worker/",
        "processing_worker": "/processing-worker/",
        "picker": "/fbs/tsd/",
        "reachtruck_driver": "/reachtruck/",
        "super_car": "/super-car/",
        "developer": "/dev/",
        "admin": "/admin/",
        "director": "/cabinet/director/",
    }
    if role in mapping:
        return mapping[role]
    # Client portal always lands in the new LK (classic /cabinet/client/ retired).
    if role == "client" or client_id is not None:
        if client_id:
            return f"/client/dashboard/lk/?client={int(client_id)}"
        return "/client/dashboard/lk/"
    if role:
        return f"/cabinet/{role}/"
    return "/"
