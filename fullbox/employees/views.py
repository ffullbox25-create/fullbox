import base64
from io import BytesIO

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.views import redirect_to_login
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden
from django.utils import timezone

from openpyxl import Workbook
from PIL import Image
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.decorators.http import require_GET, require_POST

from .badges import (
    WAREHOUSE_QR_ROLES,
    active_badge_for_employee,
    issue_badge,
    revoke_badge,
    set_qr_login_enabled,
)
from .models import Employee, EmployeeBadgeEvent
from employees.access import (
    RoleRequiredMixin,
    get_request_role,
    get_request_roles,
    is_developer_login,
    request_has_any_role,
    resolve_cabinet_url,
    role_required,
)


ROLE_USERNAMES = {
    "admin": "admin",
    "director": "director",
    "accountant": "accountant",
    "hr": "hr",
    "head_manager": "head",
    "processing_head": "processing_head",
    "packer": "packer",
    "processing_worker": "processing_worker",
    "manager": "manager",
    "storekeeper": "storekeeper",
    "logistician": "logistician",
    "driver": "driver",
    "reachtruck_driver": "reachtruck_driver",
    "super_car": "super_car",
    "picker": "picker",
    "developer": "dev",
}

PROCESSING_STAFF_ROLES = {"packer", "processing_worker"}
PRIVILEGED_EMPLOYEE_ROLES = {"admin", "director", "developer"}
EMPLOYEE_ROLE_SECURITY_LEVEL = {
    "admin": 100,
    "director": 90,
    "developer": 90,
    "hr": 80,
    "head_manager": 70,
    "accountant": 60,
    "processing_head": 50,
    "manager": 50,
    "logistician": 50,
    "storekeeper": 40,
    "fbs_controller": 40,
    "packer": 40,
    "processing_worker": 40,
    "driver": 30,
    "reachtruck_driver": 30,
    "super_car": 30,
    "picker": 30,
}


def _request_security_roles(request) -> set[str]:
    roles = set(get_request_roles(request))
    if is_developer_login(getattr(request, "user", None)):
        roles.add("developer")
    return roles


def _security_level(roles) -> int:
    return max((EMPLOYEE_ROLE_SECURITY_LEVEL.get(str(role), 0) for role in roles), default=0)


def _employee_security_roles(employee: Employee) -> set[str]:
    roles = {str(getattr(employee, "role", "") or "").strip()}
    roles.update(employee.normalized_access_roles())
    user = getattr(employee, "user", None)
    if user is not None and getattr(user, "is_superuser", False):
        roles.add("admin")
    if user is not None and getattr(user, "username", "") == "dev":
        roles.add("developer")
    roles.discard("")
    return roles


def _can_edit_employee(request, employee: Employee) -> bool:
    actor_roles = _request_security_roles(request)
    if "admin" in actor_roles:
        return True
    target_roles = _employee_security_roles(employee)
    if target_roles.intersection(PRIVILEGED_EMPLOYEE_ROLES):
        return False
    return _security_level(actor_roles) >= _security_level(target_roles)


def _assignable_employee_roles(request) -> set[str]:
    actor_roles = _request_security_roles(request)
    actor_level = _security_level(actor_roles)
    actor_is_admin = "admin" in actor_roles
    return {
        role
        for role, _label in Employee.ROLE_CHOICES
        if (actor_is_admin or role not in PRIVILEGED_EMPLOYEE_ROLES)
        and (actor_is_admin or EMPLOYEE_ROLE_SECURITY_LEVEL.get(role, 0) <= actor_level)
    }


def _build_employee_username(*, role: str) -> str:
    User = get_user_model()
    base = ROLE_USERNAMES.get(role) or (role or "user")
    username = base
    counter = 1
    while User.objects.filter(username=username).exists():
        counter += 1
        username = f"{base}{counter}"
    return username


def _delete_user_sessions(*, user_id: int) -> int:
    session_keys = []
    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator(chunk_size=500):
        session_user_id = session.get_decoded().get("_auth_user_id")
        if str(session_user_id or "") == str(user_id):
            session_keys.append(session.session_key)
    if not session_keys:
        return 0
    deleted_count, _details = Session.objects.filter(session_key__in=session_keys).delete()
    return deleted_count


def _save_employee_user(*, employee: Employee, raw_password: str, raw_username: str = "") -> None:
    username = str(raw_username or "").strip()
    password = str(raw_password or "")
    if employee.user_id:
        user = employee.user
        update_fields = []
        if username and user.username != username:
            user.username = username
            update_fields.append("username")
        email = employee.email or ""
        if user.email != email:
            user.email = email
            update_fields.append("email")
        if user.is_active != employee.is_active:
            user.is_active = employee.is_active
            update_fields.append("is_active")
        if password:
            user.set_password(password)
            update_fields.append("password")
        if update_fields:
            user.save(update_fields=update_fields)
        if not employee.is_active:
            _delete_user_sessions(user_id=user.id)
        return
    if not password:
        return
    User = get_user_model()
    user = User.objects.create_user(
        username=username or _build_employee_username(role=employee.role),
        password=password,
        email=employee.email or "",
        is_active=employee.is_active,
    )
    employee.user = user
    employee.save(update_fields=["user"])


class EmployeeForm(forms.ModelForm):
    access_roles = forms.MultipleChoiceField(
        required=False,
        label="Дополнительные роли",
        choices=Employee.ACCESS_ROLE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        help_text="Основная роль и стартовый кабинет не изменятся.",
    )
    account_username = forms.CharField(
        required=False,
        strip=True,
        label="Логин",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "username",
            }
        ),
    )
    account_password = forms.CharField(
        required=False,
        strip=False,
        label="Пароль",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "new-password",
                "data-password-input": "1",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        self.allow_access_roles = bool(kwargs.pop("allow_access_roles", False))
        self.allow_processing_staff_roles = bool(
            kwargs.pop("allow_processing_staff_roles", False)
        )
        allowed_role_keys = kwargs.pop("allowed_role_keys", None)
        self.allowed_role_keys = (
            {str(role) for role in allowed_role_keys}
            if allowed_role_keys is not None
            else {key for key, _label in Employee.ROLE_CHOICES}
        )
        super().__init__(*args, **kwargs)
        current_role = str(getattr(self.instance, "role", "") or "").strip()
        self.fields["role"].choices = [
            (key, label)
            for key, label in Employee.ROLE_CHOICES
            if key in self.allowed_role_keys
            and (
                self.allow_processing_staff_roles
                or key not in PROCESSING_STAFF_ROLES
                or key == current_role
            )
        ]
        username_field = self.fields["account_username"]
        password_field = self.fields["account_password"]
        if self.instance and getattr(self.instance, "user_id", None):
            username_field.initial = self.instance.user.username
            username_field.widget.attrs["placeholder"] = "Введите логин сотрудника"
            password_field.label = "Новый пароль"
            password_field.widget.attrs["placeholder"] = "Введите новый временный пароль"
        else:
            username_field.widget.attrs["placeholder"] = "Оставьте пустым, если нужна автогенерация"
            password_field.label = "Пароль"
            password_field.widget.attrs["placeholder"] = "Введите пароль для учетной записи сотрудника"
        self.fields["access_roles"].initial = list(self.instance.normalized_access_roles())
        if not self.allow_access_roles:
            self.fields["access_roles"].disabled = True

    class Meta:
        model = Employee
        fields = (
            "full_name",
            "role",
            "access_roles",
            "email",
            "phone",
            "facsimile",
            "is_active",
        )
        widgets = {
            "facsimile": forms.ClearableFileInput(attrs={"accept": "image/png"}),
        }

    def clean_facsimile(self):
        facsimile = self.cleaned_data.get("facsimile")
        if not facsimile:
            return facsimile
        content_type = getattr(facsimile, "content_type", "")
        if content_type and content_type.lower() != "image/png":
            raise ValidationError("Факсимиле нужно загрузить в формате PNG.")
        try:
            facsimile.seek(0)
            image = Image.open(facsimile)
            if image.format != "PNG":
                raise ValidationError("Факсимиле нужно загрузить в формате PNG.")
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError("Не удалось прочитать PNG-файл.") from exc
        finally:
            facsimile.seek(0)
        return facsimile

    def clean_account_username(self):
        username = str(self.cleaned_data.get("account_username") or "").strip()
        if not username:
            return ""
        User = get_user_model()
        queryset = User.objects.filter(username__iexact=username)
        if self.instance and getattr(self.instance, "user_id", None):
            queryset = queryset.exclude(pk=self.instance.user_id)
        if queryset.exists():
            raise ValidationError("Логин уже занят.")
        return username

    def clean_account_password(self):
        password = str(self.cleaned_data.get("account_password") or "")
        if not password:
            return ""
        user = None
        if self.instance and getattr(self.instance, "user_id", None):
            user = self.instance.user
        if user is None:
            user = get_user_model()(
                username=str(self.cleaned_data.get("account_username") or "").strip(),
                email=str(self.data.get("email") or "").strip(),
            )
        try:
            validate_password(password, user=user)
        except ValidationError as exc:
            raise ValidationError(exc.messages) from exc
        return password

    def clean_access_roles(self):
        if not self.allow_access_roles:
            return list(self.instance.normalized_access_roles())
        primary_role = str(self.cleaned_data.get("role") or "").strip()
        selected = self.cleaned_data.get("access_roles") or []
        return [
            key
            for key, _label in Employee.ACCESS_ROLE_CHOICES
            if key in selected and key != primary_role
        ]

    def clean_role(self):
        role = str(self.cleaned_data.get("role") or "").strip()
        original_role = str(getattr(self.instance, "role", "") or "").strip()
        if role not in self.allowed_role_keys:
            raise ValidationError("Недостаточно прав для назначения этой роли.")
        if not self.allow_processing_staff_roles and (
            role in PROCESSING_STAFF_ROLES
            or original_role in PROCESSING_STAFF_ROLES
        ):
            raise ValidationError(
                "Роли упаковщиц и обработчиков назначает только отдел кадров."
            )
        return role

    def save(self, commit=True):
        employee = super().save(commit=False)
        employee.access_roles = list(self.cleaned_data.get("access_roles") or [])
        if commit:
            employee.save()
            self.save_m2m()
        return employee


def _can_manage_access_roles(request) -> bool:
    return is_developer_login(request.user) or request_has_any_role(request, {"hr", "director", "admin"})


def _can_manage_processing_staff(request) -> bool:
    return request_has_any_role(request, {"hr"})


def _can_manage_badges(request) -> bool:
    return bool(
        request.user.is_authenticated
        and request_has_any_role(request, {"hr", "director", "admin"})
    )


def _employee_form_context(
    request,
    *,
    employee: Employee,
    form: EmployeeForm,
    page_title: str,
    submit_label: str,
    badge_qr_data_uri: str = "",
    badge_error: str = "",
):
    active_badge = active_badge_for_employee(employee) if employee.pk else None
    enabled_event = None
    if employee.pk and employee.qr_login_enabled:
        enabled_event = (
            EmployeeBadgeEvent.objects.filter(
                employee=employee,
                event_type=EmployeeBadgeEvent.EVENT_ACCESS_ENABLED,
            )
            .order_by("-created_at", "-id")
            .first()
        )
    return {
        "employee": employee,
        "form": form,
        "cabinet_url": resolve_cabinet_url(get_request_role(request)),
        "page_title": page_title,
        "submit_label": submit_label,
        "signature_required": employee.role in {"manager", "storekeeper"},
        "can_manage_access_roles": _can_manage_access_roles(request),
        "can_manage_badges": _can_manage_badges(request),
        "qr_role_allowed": employee.role in WAREHOUSE_QR_ROLES,
        "active_badge": active_badge,
        "qr_enabled_event": enabled_event,
        "badge_qr_data_uri": badge_qr_data_uri,
        "badge_error": badge_error,
    }


def _badge_qr_data_uri(raw_token: str) -> str:
    import qrcode

    image = qrcode.make(raw_token)
    output = BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _badge_access_denied(request):
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), "/login/")
    return HttpResponseForbidden("Управлять QR-бейджами может только ОК, директор или администратор.")


def _render_badge_error(request, *, employee: Employee, message: str, status: int = 400):
    form = EmployeeForm(
        instance=employee,
        allow_access_roles=_can_manage_access_roles(request),
        allow_processing_staff_roles=_can_manage_processing_staff(request),
        allowed_role_keys=_assignable_employee_roles(request),
    )
    response = render(
        request,
        "employees/employee_form.html",
        _employee_form_context(
            request,
            employee=employee,
            form=form,
            page_title="Редактирование сотрудника",
            submit_label="Сохранить",
            badge_error=message,
        ),
        status=status,
    )
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response


@require_POST
def employee_badge_issue(request, pk: int):
    if not _can_manage_badges(request):
        return _badge_access_denied(request)
    employee = get_object_or_404(Employee.objects.select_related("user"), pk=pk)
    if employee.role not in WAREHOUSE_QR_ROLES:
        return _render_badge_error(
            request,
            employee=employee,
            message="QR-бейджи выдаются только сотрудникам складских ролей.",
        )
    try:
        _badge, raw_token = issue_badge(employee=employee, actor=request.user)
    except ValueError as exc:
        employee.refresh_from_db()
        return _render_badge_error(request, employee=employee, message=str(exc))
    employee.refresh_from_db()
    form = EmployeeForm(
        instance=employee,
        allow_access_roles=_can_manage_access_roles(request),
        allow_processing_staff_roles=_can_manage_processing_staff(request),
        allowed_role_keys=_assignable_employee_roles(request),
    )
    response = render(
        request,
        "employees/employee_form.html",
        _employee_form_context(
            request,
            employee=employee,
            form=form,
            page_title="Редактирование сотрудника",
            submit_label="Сохранить",
            badge_qr_data_uri=_badge_qr_data_uri(raw_token),
        ),
    )
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response


@require_POST
def employee_badge_toggle(request, pk: int):
    if not _can_manage_badges(request):
        return _badge_access_denied(request)
    employee = get_object_or_404(Employee, pk=pk)
    try:
        set_qr_login_enabled(
            employee=employee,
            enabled=request.POST.get("enabled") == "1",
            actor=request.user,
        )
    except ValueError as exc:
        employee.refresh_from_db()
        return _render_badge_error(request, employee=employee, message=str(exc))
    return redirect("employees:edit", pk=pk)


@require_POST
def employee_badge_revoke(request, pk: int):
    if not _can_manage_badges(request):
        return _badge_access_denied(request)
    employee = get_object_or_404(Employee, pk=pk)
    revoke_badge(employee=employee, actor=request.user)
    return redirect("employees:edit", pk=pk)


@role_required("hr", "head_manager", "director", "admin")
@require_GET
def employee_facsimile(request, pk: int):
    employee = get_object_or_404(Employee, pk=pk)
    if not employee.facsimile:
        raise Http404("Факсимиле не найдено")
    try:
        file_handle = employee.facsimile.open("rb")
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise Http404("Факсимиле не найдено") from exc
    response = FileResponse(file_handle, content_type="image/png")
    response["Content-Disposition"] = 'inline; filename="facsimile.png"'
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@role_required("hr", "head_manager", "director", "admin")
def employee_list(request):
    filter_q = (request.GET.get("q") or "").strip()
    filter_role = (request.GET.get("role") or "").strip()
    filter_status = (request.GET.get("status") or "").strip()

    employees = Employee.objects.select_related("user")
    if filter_q:
        employees = employees.filter(full_name__icontains=filter_q)
    if filter_role:
        employees = employees.filter(role=filter_role)
    if filter_status == "active":
        employees = employees.filter(is_active=True)
    elif filter_status == "inactive":
        employees = employees.filter(is_active=False)

    stats = {
        "total": employees.count(),
        "active": employees.filter(is_active=True).count(),
        "inactive": employees.filter(is_active=False).count(),
    }
    role = get_request_role(request)
    can_edit = is_developer_login(request.user) or request_has_any_role(
        request,
        {"hr", "head_manager", "director", "admin"},
    )
    can_manage_processing_staff = _can_manage_processing_staff(request)
    employees = list(employees)
    for employee in employees:
        employee.can_edit_in_request = bool(
            can_edit
            and _can_edit_employee(request, employee)
            and (
                employee.role not in PROCESSING_STAFF_ROLES
                or can_manage_processing_staff
            )
        )
    can_enter_cabinet = is_developer_login(request.user)
    cabinet_url = resolve_cabinet_url(role)
    return render(
        request,
        "employees/employee_list.html",
        {
            "employees": employees,
            "stats": stats,
            "can_edit": can_edit,
            "can_enter_cabinet": can_enter_cabinet,
            "cabinet_url": cabinet_url,
            "filter_q": filter_q,
            "filter_role": filter_role,
            "filter_status": filter_status,
            "role_choices": Employee.ROLE_CHOICES,
        },
    )


def employee_enter_cabinet(request, pk: int):
    if not is_developer_login(request.user):
        return HttpResponseForbidden("Доступ запрещен")
    employee = get_object_or_404(Employee, pk=pk, is_active=True)
    request.session["developer_impersonated_employee_id"] = employee.id
    request.session["employee_id"] = employee.id
    request.session["employee_role"] = employee.role
    request.session["employee_name"] = employee.full_name
    request.session.modified = True
    return redirect(resolve_cabinet_url(employee.role))


@role_required("hr", "head_manager", "director", "admin")
def employee_export_logins(request):
    if not is_developer_login(request.user) and not request_has_any_role(
        request,
        {"hr", "head_manager", "director", "admin"},
    ):
        return redirect("/employees/")
    employees = Employee.objects.select_related("user").order_by("full_name")

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Сотрудники"
    sheet.append(
        [
            "ID",
            "ФИО",
            "Роль",
            "Дополнительные роли",
            "Email",
            "Телефон",
            "Логин",
            "Активен",
        ]
    )
    for employee in employees:
        sheet.append(
            [
                employee.id,
                employee.full_name or "",
                employee.get_role_display() or "",
                ", ".join(employee.access_role_labels),
                employee.email or "",
                employee.phone or "",
                employee.user.username if employee.user else "",
                "Да" if employee.is_active else "Нет",
            ]
        )

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="employee_logins_{timezone.localdate().isoformat()}.xlsx"'
    )
    return response


class EmployeeCreateView(RoleRequiredMixin, View):
    allowed_roles = ("hr", "head_manager", "director", "admin")
    template_name = "employees/employee_form.html"

    def get(self, request):
        form = EmployeeForm(
            allow_access_roles=_can_manage_access_roles(request),
            allow_processing_staff_roles=_can_manage_processing_staff(request),
            allowed_role_keys=_assignable_employee_roles(request),
        )
        return render(
            request,
            self.template_name,
            _employee_form_context(
                request,
                employee=Employee(),
                form=form,
                page_title="Создание сотрудника",
                submit_label="Создать",
            ),
        )

    def post(self, request):
        form = EmployeeForm(
            request.POST,
            request.FILES,
            allow_access_roles=_can_manage_access_roles(request),
            allow_processing_staff_roles=_can_manage_processing_staff(request),
            allowed_role_keys=_assignable_employee_roles(request),
        )
        if form.is_valid():
            with transaction.atomic():
                employee = form.save()
                _save_employee_user(
                    employee=employee,
                    raw_password=form.cleaned_data.get("account_password"),
                    raw_username=form.cleaned_data.get("account_username"),
                )
            return redirect("/employees/")
        return render(
            request,
            self.template_name,
            _employee_form_context(
                request,
                employee=Employee(role=form.data.get("role") or ""),
                form=form,
                page_title="Создание сотрудника",
                submit_label="Создать",
            ),
        )


class EmployeeEditView(RoleRequiredMixin, View):
    allowed_roles = ("hr", "head_manager", "director", "admin")
    template_name = "employees/employee_form.html"

    def get(self, request, pk: int):
        employee = get_object_or_404(Employee.objects.select_related("user"), pk=pk)
        if not _can_edit_employee(request, employee):
            return HttpResponseForbidden(
                "Недостаточно прав для изменения учётной записи этого сотрудника."
            )
        if (
            employee.role in PROCESSING_STAFF_ROLES
            and not _can_manage_processing_staff(request)
        ):
            return HttpResponseForbidden(
                "Роли упаковщиц и обработчиков изменяет только отдел кадров."
            )
        form = EmployeeForm(
            instance=employee,
            allow_access_roles=_can_manage_access_roles(request),
            allow_processing_staff_roles=_can_manage_processing_staff(request),
            allowed_role_keys=_assignable_employee_roles(request),
        )
        return render(
            request,
            self.template_name,
            _employee_form_context(
                request,
                employee=employee,
                form=form,
                page_title="Редактирование сотрудника",
                submit_label="Сохранить",
            ),
        )

    def post(self, request, pk: int):
        employee = get_object_or_404(Employee.objects.select_related("user"), pk=pk)
        if not _can_edit_employee(request, employee):
            return HttpResponseForbidden(
                "Недостаточно прав для изменения учётной записи этого сотрудника."
            )
        if (
            employee.role in PROCESSING_STAFF_ROLES
            and not _can_manage_processing_staff(request)
        ):
            return HttpResponseForbidden(
                "Роли упаковщиц и обработчиков изменяет только отдел кадров."
            )
        form = EmployeeForm(
            request.POST,
            request.FILES,
            instance=employee,
            allow_access_roles=_can_manage_access_roles(request),
            allow_processing_staff_roles=_can_manage_processing_staff(request),
            allowed_role_keys=_assignable_employee_roles(request),
        )
        if form.is_valid():
            with transaction.atomic():
                employee = form.save()
                _save_employee_user(
                    employee=employee,
                    raw_password=form.cleaned_data.get("account_password"),
                    raw_username=form.cleaned_data.get("account_username"),
                )
            return redirect("/employees/")
        return render(
            request,
            self.template_name,
            _employee_form_context(
                request,
                employee=employee,
                form=form,
                page_title="Редактирование сотрудника",
                submit_label="Сохранить",
            ),
        )
