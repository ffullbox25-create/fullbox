import os
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from employees.access import (
    RoleRequiredMixin,
    get_request_role,
    request_has_any_role,
    resolve_cabinet_url,
)
from employees.models import Employee

from .exports import build_monthly_payroll_workbook, workbook_bytes
from .forms import (
    HrDepartmentForm,
    HrEmployeePayRateForm,
    HrEmployeeProfileForm,
    HrPayrollAdjustmentForm,
    HrTimesheetForm,
)
from .models import (
    HrDepartment,
    HrEmployeePayRate,
    HrEmployeeProfile,
    HrPayrollAdjustment,
    HrPayrollAuditEvent,
    HrTimesheetEntry,
)
from .reporting import build_monthly_payroll_report, parse_report_month
from .services import record_payroll_audit, save_payroll_object


INTEGRATION_CATALOG = (
    {
        "key": "hh",
        "name": "hh.ru",
        "short_name": "hh",
        "summary": "Вакансии, отклики, статусы кандидатов и переписка с соискателями.",
        "connection_type": "OAuth 2.0",
        "sync_mode": "Webhook",
        "required_settings": ("HR_HH_CLIENT_ID", "HR_HH_CLIENT_SECRET"),
        "capabilities": (
            "Загрузка новых откликов",
            "Синхронизация статусов кандидата",
            "Переписка по отклику",
            "Связь отклика с вакансией Fullbox",
        ),
        "setup_steps": (
            "Зарегистрировать приложение работодателя на dev.hh.ru.",
            "Добавить Client ID и Client Secret в защищённые настройки Fullbox.",
            "Авторизовать аккаунт менеджера работодателя через OAuth 2.0.",
            "Включить webhook новых откликов и сообщений.",
        ),
        "documentation_url": "https://api.hh.ru/openapi/redoc",
        "tone": "red",
    },
    {
        "key": "avito",
        "name": "Авито Работа",
        "short_name": "A",
        "summary": "Вакансии, отклики, результаты первичного опроса и сообщения Авито.",
        "connection_type": "API для бизнеса",
        "sync_mode": "Webhook",
        "required_settings": ("HR_AVITO_CLIENT_ID", "HR_AVITO_CLIENT_SECRET"),
        "capabilities": (
            "Загрузка откликов в реальном времени",
            "Ответы встроенного чат-бота",
            "Чаты с кандидатами",
            "Публикация и обновление вакансий",
        ),
        "setup_steps": (
            "Проверить доступ к API в профессиональном кабинете Авито.",
            "Получить Client ID и Client Secret.",
            "Добавить ключи в защищённые настройки Fullbox.",
            "Включить webhook откликов и сообщений.",
        ),
        "documentation_url": "https://developers.avito.ru/api-catalog/job/documentation",
        "tone": "blue",
    },
    {
        "key": "superjob",
        "name": "SuperJob",
        "short_name": "SJ",
        "summary": "Импорт вакансий и откликов для единой очереди кандидатов.",
        "connection_type": "API приложения",
        "sync_mode": "По расписанию",
        "required_settings": ("HR_SUPERJOB_APP_ID",),
        "capabilities": (
            "Получение вакансий работодателя",
            "Импорт новых откликов",
            "Проверка новых сообщений",
            "Единые статусы внутри Fullbox",
        ),
        "setup_steps": (
            "Зарегистрировать приложение SuperJob.",
            "Получить идентификатор приложения.",
            "Добавить ключ в защищённые настройки Fullbox.",
            "Настроить периодическую синхронизацию.",
        ),
        "documentation_url": "https://api.superjob.ru/",
        "tone": "green",
    },
    {
        "key": "rabota",
        "name": "Работа.ру",
        "short_name": "Р",
        "summary": "Отдельный канал массового подбора с подключением через партнёрский API.",
        "connection_type": "Партнёрский доступ",
        "sync_mode": "После согласования",
        "required_settings": ("HR_RABOTA_CLIENT_ID", "HR_RABOTA_CLIENT_SECRET"),
        "capabilities": (
            "Связь внешних и внутренних вакансий",
            "Импорт откликов",
            "Защита от дублей кандидатов",
            "Передача кандидата HR-специалисту",
        ),
        "setup_steps": (
            "Запросить доступ к интеграции для аккаунта работодателя.",
            "Получить параметры подключения.",
            "Добавить их в защищённые настройки Fullbox.",
            "Провести тестовую синхронизацию одной вакансии.",
        ),
        "documentation_url": "https://www.rabota.ru/",
        "tone": "orange",
    },
)


def _integration_rows():
    rows = []
    for item in INTEGRATION_CATALOG:
        row = dict(item)
        row["credentials_ready"] = all(
            bool(getattr(settings, setting_name, None) or os.environ.get(setting_name, ""))
            for setting_name in item["required_settings"]
        )
        rows.append(row)
    return rows


class HrBaseView(RoleRequiredMixin, TemplateView):
    allowed_roles = ("hr", "director", "admin")
    active_nav = ""

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            {
                "active_nav": self.active_nav,
                "cabinet_url": resolve_cabinet_url(get_request_role(self.request)),
                "can_manage_employees": request_has_any_role(
                    self.request, {"hr", "director", "admin"}
                ),
                "can_view_integrations": request_has_any_role(
                    self.request, {"hr", "director", "admin"}
                ),
                "can_manage_payroll_setup": request_has_any_role(
                    self.request, {"hr", "admin"}
                ),
                "can_manage_payroll_adjustments": request_has_any_role(
                    self.request, {"hr", "accountant", "admin"}
                ),
            }
        )
        return ctx


class HrDashboardView(HrBaseView):
    template_name = "hr/dashboard.html"
    active_nav = "dashboard"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        employees = Employee.objects.select_related("user")
        role_rows = (
            employees.values("role")
            .annotate(
                total=Count("id"),
                active=Count("id", filter=Q(is_active=True)),
                inactive=Count("id", filter=Q(is_active=False)),
            )
            .order_by("role")
        )
        role_labels = dict(Employee.ROLE_CHOICES)
        ctx.update(
            {
                "stats": {
                    "total": employees.count(),
                    "active": employees.filter(is_active=True).count(),
                    "inactive": employees.filter(is_active=False).count(),
                    "without_user": employees.filter(user__isnull=True).count(),
                    "with_facsimile": employees.filter(facsimile__isnull=False).exclude(facsimile="").count(),
                },
                "role_rows": [
                    {
                        "label": role_labels.get(row["role"], row["role"] or "Без роли"),
                        "total": row["total"],
                        "active": row["active"],
                        "inactive": row["inactive"],
                    }
                    for row in role_rows
                ],
                "recent_employees": employees.order_by("-updated_at")[:8],
            }
        )
        return ctx


class HrIntegrationsView(HrBaseView):
    template_name = "hr/integrations.html"
    active_nav = "integrations"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        integrations = _integration_rows()
        ctx.update(
            {
                "integrations": integrations,
                "integration_stats": {
                    "total": len(integrations),
                    "credentials_ready": sum(
                        1 for item in integrations if item["credentials_ready"]
                    ),
                    "webhook": sum(
                        1 for item in integrations if item["sync_mode"] == "Webhook"
                    ),
                },
            }
        )
        return ctx


class HrIntegrationDetailView(HrBaseView):
    template_name = "hr/integration_detail.html"
    active_nav = "integrations"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        integration_key = kwargs.get("integration_key")
        integration = next(
            (
                item
                for item in _integration_rows()
                if item["key"] == integration_key
            ),
            None,
        )
        if integration is None:
            raise Http404("Площадка не найдена")
        ctx["integration"] = integration
        return ctx


class HrPayrollBaseView(HrBaseView):
    allowed_roles = ("hr", "director", "accountant", "admin")
    active_nav = "payroll-report"


class HrPayrollSetupView(HrBaseView):
    allowed_roles = ("hr", "admin")


class HrPayrollDepartmentsView(HrPayrollSetupView):
    template_name = "hr/payroll_departments.html"
    active_nav = "payroll-departments"

    def _edited_department(self):
        raw_id = self.request.GET.get("edit") or self.request.POST.get("department_id")
        if not raw_id:
            return None
        return get_object_or_404(HrDepartment, pk=raw_id)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        edited = self._edited_department()
        ctx.update(
            {
                "departments": HrDepartment.objects.annotate(
                    employee_count=Count("employee_profiles")
                ).order_by("name"),
                "edited_department": edited,
                "form": kwargs.get("form") or HrDepartmentForm(instance=edited),
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        edited = self._edited_department()
        form = HrDepartmentForm(request.POST, instance=edited)
        if form.is_valid():
            save_payroll_object(form.save(commit=False), actor=request.user)
            messages.success(request, "Отдел сохранён.")
            return redirect("hr:payroll-departments")
        return self.render_to_response(self.get_context_data(form=form))


class HrPayrollEmployeesView(HrPayrollSetupView):
    template_name = "hr/payroll_employees.html"
    active_nav = "payroll-employees"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        employees = Employee.objects.filter(is_active=True).select_related(
            "hr_profile", "hr_profile__department"
        ).prefetch_related("hr_pay_rates").order_by("full_name", "id")
        rows = []
        for employee in employees:
            profile = getattr(employee, "hr_profile", None)
            rates = list(employee.hr_pay_rates.all())
            rows.append(
                {
                    "employee": employee,
                    "profile": profile,
                    "has_rate": bool(rates),
                    "latest_rate": rates[0] if rates else None,
                    "is_complete": bool(profile and rates),
                }
            )
        ctx["rows"] = rows
        ctx["complete_count"] = sum(1 for row in rows if row["is_complete"])
        return ctx


class HrPayrollEmployeeDetailView(HrPayrollSetupView):
    template_name = "hr/payroll_employee_detail.html"
    active_nav = "payroll-employees"

    def _employee(self):
        return get_object_or_404(Employee, pk=self.kwargs["employee_id"], is_active=True)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        employee = self._employee()
        profile = HrEmployeeProfile.objects.filter(employee=employee).first()
        ctx.update(
            {
                "employee": employee,
                "profile_form": kwargs.get("profile_form")
                or HrEmployeeProfileForm(instance=profile, employee=employee),
                "rate_form": kwargs.get("rate_form")
                or HrEmployeePayRateForm(
                    initial={"effective_from": timezone.localdate().replace(day=1)}
                ),
                "rates": employee.hr_pay_rates.order_by("-effective_from", "-id"),
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        employee = self._employee()
        action = request.POST.get("action")
        if action == "profile":
            profile = HrEmployeeProfile.objects.filter(employee=employee).first()
            form = HrEmployeeProfileForm(
                request.POST,
                instance=profile,
                employee=employee,
            )
            if form.is_valid():
                instance = form.save(commit=False)
                instance.employee = employee
                save_payroll_object(instance, actor=request.user)
                messages.success(request, "Карточка сотрудника сохранена.")
                return redirect("hr:payroll-employee-detail", employee_id=employee.pk)
            return self.render_to_response(self.get_context_data(profile_form=form))
        if action == "rate":
            form = HrEmployeePayRateForm(
                request.POST, instance=HrEmployeePayRate(employee=employee)
            )
            if form.is_valid():
                instance = form.save(commit=False)
                instance.employee = employee
                try:
                    save_payroll_object(instance, actor=request.user)
                except ValidationError as exc:
                    form.add_error(None, exc)
                else:
                    messages.success(request, "Ставка добавлена в историю.")
                    return redirect("hr:payroll-employee-detail", employee_id=employee.pk)
            return self.render_to_response(self.get_context_data(rate_form=form))
        return HttpResponse(status=400)


class HrPayrollTimesheetView(HrPayrollSetupView):
    template_name = "hr/payroll_timesheet.html"
    active_nav = "payroll-timesheet"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        month = parse_report_month(self.request.GET.get("month") or self.request.POST.get("month"))
        if month.month == 12:
            next_month = month.replace(year=month.year + 1, month=1)
        else:
            next_month = month.replace(month=month.month + 1)
        entries = HrTimesheetEntry.objects.filter(
            work_date__gte=month, work_date__lt=next_month
        ).select_related("employee").order_by("-work_date", "employee__full_name")[:300]
        ctx.update(
            {
                "month_value": month.strftime("%Y-%m"),
                "form": kwargs.get("form")
                or HrTimesheetForm(initial={"work_date": timezone.localdate()}),
                "entries": [
                    {
                        "item": entry,
                        "planned_hours": Decimal(entry.planned_minutes) / 60,
                        "worked_hours": Decimal(entry.worked_minutes) / 60,
                        "internship_hours": Decimal(entry.internship_minutes) / 60,
                    }
                    for entry in entries
                ],
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        form = HrTimesheetForm(request.POST)
        if form.is_valid():
            entry = form.save(actor=request.user)
            messages.success(request, "Строка табеля сохранена. Повторный ввод за день обновляет строку.")
            return redirect(
                f"{reverse('hr:payroll-timesheet')}?month={entry.work_date:%Y-%m}"
            )
        return self.render_to_response(self.get_context_data(form=form))


class HrPayrollAdjustmentsView(HrPayrollBaseView):
    template_name = "hr/payroll_adjustments.html"
    active_nav = "payroll-adjustments"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        month = parse_report_month(self.request.GET.get("month") or self.request.POST.get("month"))
        ctx.update(
            {
                "month_value": month.strftime("%Y-%m"),
                "form": kwargs.get("form")
                or HrPayrollAdjustmentForm(initial={"month": month}),
                "adjustments": HrPayrollAdjustment.objects.filter(month=month)
                .select_related("employee", "created_by", "approved_by")
                .order_by("-created_at", "-id")[:300],
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        if not request_has_any_role(request, {"hr", "accountant", "admin"}):
            return HttpResponseForbidden("Доступ запрещен")
        form = HrPayrollAdjustmentForm(request.POST)
        if form.is_valid():
            adjustment = form.save(commit=False)
            adjustment.created_by = request.user
            if adjustment.status == HrPayrollAdjustment.STATUS_APPROVED:
                adjustment.approved_by = request.user
            save_payroll_object(adjustment, actor=request.user)
            messages.success(request, "Начисление сохранено.")
            return redirect(
                f"{reverse('hr:payroll-adjustments')}?month={adjustment.month:%Y-%m}"
            )
        return self.render_to_response(self.get_context_data(form=form))


class HrPayrollAdjustmentApproveView(RoleRequiredMixin, View):
    allowed_roles = ("hr", "accountant", "admin")

    def post(self, request, *args, **kwargs):
        adjustment = get_object_or_404(
            HrPayrollAdjustment, pk=kwargs["adjustment_id"]
        )
        adjustment.status = HrPayrollAdjustment.STATUS_APPROVED
        adjustment.approved_by = request.user
        save_payroll_object(adjustment, actor=request.user)
        messages.success(request, "Начисление подтверждено и включено в отчёт.")
        return redirect(
            f"{reverse('hr:payroll-adjustments')}?month={adjustment.month:%Y-%m}"
        )


class HrMonthlyPayrollReportView(HrPayrollBaseView):
    template_name = "hr/monthly_payroll_report.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        month = parse_report_month(self.request.GET.get("month"))
        report = build_monthly_payroll_report(month)
        ctx.update(
            {
                "report": report,
                "export_url": f"/hr/reports/monthly/export/?month={report['month_value']}",
            }
        )
        return ctx


class HrMonthlyPayrollExportView(RoleRequiredMixin, View):
    allowed_roles = ("hr", "director", "accountant", "admin")

    def get(self, request, *args, **kwargs):
        month = parse_report_month(request.GET.get("month"))
        report = build_monthly_payroll_report(month)
        content = workbook_bytes(build_monthly_payroll_workbook(report))
        record_payroll_audit(
            action=HrPayrollAuditEvent.ACTION_EXPORT,
            object_type="MonthlyPayrollReport",
            object_id=report["month_value"],
            actor=request.user,
            month=month,
            changes={"rows": len(report["rows"]), "format": "xlsx"},
        )
        response = HttpResponse(
            content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="hr_monthly_payroll_{report["month_value"]}.xlsx"'
        )
        return response


class HrPayrollAuditView(HrPayrollBaseView):
    template_name = "hr/payroll_audit.html"
    active_nav = "payroll-audit"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        month = parse_report_month(self.request.GET.get("month"))
        ctx.update(
            {
                "month_value": month.strftime("%Y-%m"),
                "events": HrPayrollAuditEvent.objects.filter(month=month)
                .select_related("actor", "employee")
                .order_by("-created_at", "-id")[:200],
            }
        )
        return ctx
