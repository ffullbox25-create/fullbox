from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from employees.models import Employee


class HrDepartment(models.Model):
    name = models.CharField("Название", max_length=120, unique=True)
    code = models.SlugField("Код", max_length=64, unique=True)
    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "id")
        verbose_name = "HR-отдел"
        verbose_name_plural = "HR-отделы"

    def __str__(self):
        return self.name


class HrEmployeeProfile(models.Model):
    employee = models.OneToOneField(
        Employee,
        on_delete=models.CASCADE,
        related_name="hr_profile",
        verbose_name="Сотрудник",
    )
    department = models.ForeignKey(
        HrDepartment,
        on_delete=models.PROTECT,
        related_name="employee_profiles",
        verbose_name="Отдел",
    )
    position = models.CharField("Должность", max_length=160, blank=True)
    personnel_number = models.CharField(
        "Табельный номер",
        max_length=64,
        blank=True,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("department__name", "employee__full_name")
        verbose_name = "HR-карточка сотрудника"
        verbose_name_plural = "HR-карточки сотрудников"

    def __str__(self):
        return f"{self.employee.full_name} · {self.department.name}"


class HrEmployeePayRate(models.Model):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="hr_pay_rates",
        verbose_name="Сотрудник",
    )
    effective_from = models.DateField("Действует с", db_index=True)
    effective_to = models.DateField("Действует по", null=True, blank=True, db_index=True)
    hourly_rate = models.DecimalField(
        "Почасовая ставка",
        max_digits=12,
        decimal_places=2,
        default=0,
    )
    monthly_salary = models.DecimalField(
        "Месячный оклад",
        max_digits=12,
        decimal_places=2,
        default=0,
    )
    note = models.CharField("Основание", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("employee_id", "-effective_from", "-id")
        verbose_name = "Ставка сотрудника"
        verbose_name_plural = "Ставки сотрудников"
        constraints = [
            models.UniqueConstraint(
                fields=("employee", "effective_from"),
                name="hr_unique_employee_rate_start",
            ),
            models.CheckConstraint(
                condition=models.Q(hourly_rate__gte=0),
                name="hr_hourly_rate_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(monthly_salary__gte=0),
                name="hr_monthly_salary_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gte=models.F("effective_from")),
                name="hr_rate_period_valid",
            ),
        ]

    def clean(self):
        super().clean()
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValidationError({"effective_to": "Дата окончания не может быть раньше даты начала."})
        overlap = HrEmployeePayRate.objects.filter(employee=self.employee).exclude(pk=self.pk)
        overlap = overlap.filter(
            models.Q(effective_to__isnull=True) | models.Q(effective_to__gte=self.effective_from)
        )
        if self.effective_to:
            overlap = overlap.filter(effective_from__lte=self.effective_to)
        if overlap.exists():
            raise ValidationError("Период ставки пересекается с другой ставкой сотрудника.")

    def __str__(self):
        return f"{self.employee.full_name}: {self.effective_from:%d.%m.%Y}"


class HrTimesheetEntry(models.Model):
    SOURCE_MANUAL = "manual"
    SOURCE_IMPORT = "import"
    SOURCE_CHOICES = (
        (SOURCE_MANUAL, "Вручную"),
        (SOURCE_IMPORT, "Импорт"),
    )

    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="hr_timesheet_entries",
        verbose_name="Сотрудник",
    )
    work_date = models.DateField("Дата", db_index=True)
    planned_minutes = models.PositiveIntegerField("План, минут", default=0)
    worked_minutes = models.PositiveIntegerField("Отработано, минут", default=0)
    late_minutes = models.PositiveIntegerField("Опоздание, минут", default=0)
    internship_minutes = models.PositiveIntegerField("Стажировка, минут", default=0)
    source = models.CharField("Источник", max_length=16, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    source_key = models.CharField("Ключ источника", max_length=128, blank=True)
    note = models.CharField("Комментарий", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-work_date", "employee__full_name")
        verbose_name = "Строка табеля"
        verbose_name_plural = "Строки табеля"
        constraints = [
            models.UniqueConstraint(
                fields=("employee", "work_date"),
                name="hr_unique_employee_timesheet_day",
            ),
            models.CheckConstraint(
                condition=models.Q(internship_minutes__lte=models.F("worked_minutes")),
                name="hr_internship_within_worked",
            ),
        ]
        indexes = [
            models.Index(fields=("work_date", "employee"), name="hr_timesheet_date_emp_idx"),
        ]

    def clean(self):
        super().clean()
        if self.internship_minutes > self.worked_minutes:
            raise ValidationError(
                {"internship_minutes": "Стажировка не может превышать отработанное время."}
            )

    def __str__(self):
        return f"{self.employee.full_name}: {self.work_date:%d.%m.%Y}"


class HrPayrollAdjustment(models.Model):
    KIND_ACCRUAL = "accrual"
    KIND_PREMIUM = "premium"
    KIND_CONTRACT_PAYMENT = "contract_payment"
    KIND_BONUS = "bonus"
    KIND_DEDUCTION = "deduction"
    KIND_CHOICES = (
        (KIND_ACCRUAL, "Дополнительное начисление"),
        (KIND_PREMIUM, "Премия"),
        (KIND_CONTRACT_PAYMENT, "Выплата по договору"),
        (KIND_BONUS, "Бонус"),
        (KIND_DEDUCTION, "Удержание"),
    )
    STATUS_DRAFT = "draft"
    STATUS_APPROVED = "approved"
    STATUS_CHOICES = (
        (STATUS_DRAFT, "Черновик"),
        (STATUS_APPROVED, "Подтверждено"),
    )

    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="hr_payroll_adjustments",
        verbose_name="Сотрудник",
    )
    month = models.DateField("Месяц", db_index=True, help_text="Первый день месяца")
    kind = models.CharField("Вид", max_length=32, choices=KIND_CHOICES)
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2)
    status = models.CharField(
        "Статус",
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
        db_index=True,
    )
    description = models.CharField("Основание", max_length=255)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_hr_payroll_adjustments",
        verbose_name="Создал",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_hr_payroll_adjustments",
        verbose_name="Подтвердил",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-month", "employee__full_name", "kind", "id")
        verbose_name = "Корректировка расчёта"
        verbose_name_plural = "Корректировки расчёта"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0),
                name="hr_payroll_adjustment_nonnegative",
            ),
        ]
        indexes = [
            models.Index(
                fields=("month", "status", "employee"),
                name="hr_adjust_month_status_emp_idx",
            ),
        ]

    def clean(self):
        super().clean()
        if self.month and self.month.day != 1:
            raise ValidationError({"month": "Укажите первый день месяца."})

    def __str__(self):
        return f"{self.employee.full_name}: {self.get_kind_display()}"


class HrPayrollAuditEvent(models.Model):
    ACTION_CREATE = "create"
    ACTION_UPDATE = "update"
    ACTION_DELETE = "delete"
    ACTION_APPROVE = "approve"
    ACTION_EXPORT = "export"
    ACTION_CHOICES = (
        (ACTION_CREATE, "Создание"),
        (ACTION_UPDATE, "Изменение"),
        (ACTION_DELETE, "Удаление"),
        (ACTION_APPROVE, "Подтверждение"),
        (ACTION_EXPORT, "Экспорт"),
    )

    action = models.CharField("Действие", max_length=16, choices=ACTION_CHOICES)
    object_type = models.CharField("Тип объекта", max_length=64)
    object_id = models.CharField("ID объекта", max_length=64, blank=True)
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="hr_payroll_audit_events",
        verbose_name="Сотрудник отчёта",
    )
    month = models.DateField("Месяц", null=True, blank=True, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="hr_payroll_audit_events",
        verbose_name="Инициатор",
    )
    changes = models.JSONField("Изменения", default=dict, blank=True)
    created_at = models.DateTimeField("Время", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "Событие журнала расчёта"
        verbose_name_plural = "Журнал расчёта"
        indexes = [
            models.Index(fields=("month", "created_at"), name="hr_audit_month_created_idx"),
        ]

    def __str__(self):
        return f"{self.get_action_display()} · {self.object_type}"
