from decimal import Decimal, ROUND_HALF_UP

from django import forms
from django.db.models import Q

from employees.models import Employee

from .models import (
    HrDepartment,
    HrEmployeePayRate,
    HrEmployeeProfile,
    HrPayrollAdjustment,
    HrTimesheetEntry,
)
from .services import save_payroll_object


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, attrs=None, format=None):
        super().__init__(attrs=attrs, format=format or "%Y-%m-%d")


class MonthInput(forms.DateInput):
    input_type = "month"


class HrDepartmentForm(forms.ModelForm):
    class Meta:
        model = HrDepartment
        fields = ("name", "code", "is_active")


def employee_position_choices(employee: Employee | None) -> tuple[tuple[str, str], ...]:
    if employee is None:
        return ()
    labels = []
    primary_label = str(employee.get_role_display() or employee.role or "").strip()
    if primary_label:
        labels.append(primary_label)
    for label in employee.access_role_labels:
        normalized = str(label or "").strip()
        if normalized and normalized.casefold() not in {
            value.casefold() for value in labels
        }:
            labels.append(normalized)
    return tuple((label, label) for label in labels)


class HrEmployeeProfileForm(forms.ModelForm):
    position = forms.ChoiceField(
        label="Должность",
        choices=(),
        help_text="Список сформирован из основной и дополнительных ролей сотрудника.",
        error_messages={
            "invalid_choice": "Выберите должность из назначенных сотруднику ролей."
        },
    )

    class Meta:
        model = HrEmployeeProfile
        fields = ("department", "position", "personnel_number")

    def __init__(self, *args, **kwargs):
        employee = kwargs.pop("employee", None)
        super().__init__(*args, **kwargs)
        if employee is None and self.instance and self.instance.employee_id:
            employee = self.instance.employee
        position_choices = employee_position_choices(employee)
        self.fields["position"].choices = position_choices
        if not self.is_bound and position_choices:
            allowed_positions = {value for value, _label in position_choices}
            current_position = str(getattr(self.instance, "position", "") or "").strip()
            self.initial["position"] = (
                current_position
                if current_position in allowed_positions
                else position_choices[0][0]
            )
        departments = HrDepartment.objects.order_by("name")
        if self.instance and self.instance.pk and self.instance.department_id:
            departments = departments.filter(
                Q(is_active=True) | Q(pk=self.instance.department_id)
            )
        else:
            departments = departments.filter(is_active=True)
        self.fields["department"].queryset = departments


class HrEmployeePayRateForm(forms.ModelForm):
    class Meta:
        model = HrEmployeePayRate
        fields = (
            "effective_from",
            "effective_to",
            "hourly_rate",
            "monthly_salary",
            "note",
        )
        widgets = {
            "effective_from": DateInput(),
            "effective_to": DateInput(),
        }

    def clean(self):
        cleaned = super().clean()
        hourly_rate = cleaned.get("hourly_rate") or Decimal("0")
        monthly_salary = cleaned.get("monthly_salary") or Decimal("0")
        if hourly_rate == 0 and monthly_salary == 0:
            raise forms.ValidationError("Укажите почасовую ставку или месячный оклад.")
        return cleaned


class HrTimesheetForm(forms.Form):
    employee = forms.ModelChoiceField(label="Сотрудник", queryset=Employee.objects.none())
    work_date = forms.DateField(label="Дата", widget=DateInput())
    planned_hours = forms.DecimalField(
        label="План, часов", min_value=0, max_digits=7, decimal_places=2
    )
    worked_hours = forms.DecimalField(
        label="Отработано, часов", min_value=0, max_digits=7, decimal_places=2
    )
    late_minutes = forms.IntegerField(label="Опоздание, минут", min_value=0, initial=0)
    internship_hours = forms.DecimalField(
        label="Стажировка, часов",
        min_value=0,
        max_digits=7,
        decimal_places=2,
        initial=0,
    )
    note = forms.CharField(label="Комментарий", max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.filter(is_active=True).order_by(
            "full_name", "id"
        )

    @staticmethod
    def _minutes(hours):
        return int((hours * Decimal("60")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def clean(self):
        cleaned = super().clean()
        worked_hours = cleaned.get("worked_hours")
        internship_hours = cleaned.get("internship_hours")
        if (
            worked_hours is not None
            and internship_hours is not None
            and internship_hours > worked_hours
        ):
            self.add_error(
                "internship_hours", "Стажировка не может превышать отработанное время."
            )
        return cleaned

    def save(self, *, actor):
        employee = self.cleaned_data["employee"]
        work_date = self.cleaned_data["work_date"]
        entry = HrTimesheetEntry.objects.filter(
            employee=employee, work_date=work_date
        ).first()
        if entry is None:
            entry = HrTimesheetEntry(employee=employee, work_date=work_date)
        entry.planned_minutes = self._minutes(self.cleaned_data["planned_hours"])
        entry.worked_minutes = self._minutes(self.cleaned_data["worked_hours"])
        entry.late_minutes = self.cleaned_data["late_minutes"]
        entry.internship_minutes = self._minutes(self.cleaned_data["internship_hours"])
        entry.source = HrTimesheetEntry.SOURCE_MANUAL
        entry.source_key = ""
        entry.note = self.cleaned_data["note"]
        return save_payroll_object(entry, actor=actor)


class HrPayrollAdjustmentForm(forms.ModelForm):
    month = forms.DateField(
        label="Месяц",
        input_formats=("%Y-%m",),
        widget=MonthInput(format="%Y-%m"),
    )

    class Meta:
        model = HrPayrollAdjustment
        fields = ("employee", "month", "kind", "amount", "status", "description")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.filter(is_active=True).order_by(
            "full_name", "id"
        )

    def clean_month(self):
        month = self.cleaned_data["month"]
        return month.replace(day=1)
