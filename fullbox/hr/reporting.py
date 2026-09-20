from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q, Sum
from django.utils import timezone

from employees.models import Employee
from head_manager.employee_report import warehouse_employee_metric_points

from .models import HrEmployeePayRate, HrPayrollAdjustment, HrTimesheetEntry


MONEY_STEP = Decimal("0.01")
HOUR_STEP = Decimal("0.01")
PRODUCTIVITY_METRICS = frozenset(
    {
        "receiving_cz_units",
        "palletization_units",
        "placement_units",
        "reachtruck_units",
        "fbs_replenishment_units",
        "movement_units",
        "otg_units",
        "pick_units",
        "verified_units",
        "shipment_units",
        "shipping_units",
        "processing_units",
        "processing_boxed_units",
        "inventory_units",
    }
)


def money(value) -> Decimal:
    return Decimal(value or 0).quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


def hours(minutes) -> Decimal:
    return (Decimal(minutes or 0) / Decimal(60)).quantize(HOUR_STEP, rounding=ROUND_HALF_UP)


def parse_report_month(raw_value: str | None, *, today=None) -> date:
    today = today or timezone.localdate()
    value = str(raw_value or "").strip()
    if value:
        try:
            year, month = value.split("-", 1)
            parsed = date(int(year), int(month), 1)
            if 2000 <= parsed.year <= 2100:
                return parsed
        except (TypeError, ValueError):
            pass
    return today.replace(day=1)


def month_bounds(month: date) -> tuple[date, date]:
    next_month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return month, next_month - timedelta(days=1)


def _rates_by_employee(employees, start, end):
    employee_ids = [employee.pk for employee in employees]
    rates = (
        HrEmployeePayRate.objects.filter(
            employee_id__in=employee_ids,
            effective_from__lte=end,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=start))
        .order_by("employee_id", "-effective_from", "-id")
    )
    result = defaultdict(list)
    for rate in rates:
        result[rate.employee_id].append(rate)
    return result


def _timesheets_by_employee(employees, start, end):
    rows = list(
        HrTimesheetEntry.objects.filter(
            employee__in=employees,
            work_date__gte=start,
            work_date__lte=end,
        )
        .values(
            "employee_id",
            "work_date",
            "planned_minutes",
            "worked_minutes",
            "late_minutes",
            "internship_minutes",
        )
        .order_by("employee_id", "work_date")
    )
    result = defaultdict(list)
    for row in rows:
        result[row["employee_id"]].append(row)
    return result


def _adjustment_map(employees, month):
    rows = (
        HrPayrollAdjustment.objects.filter(
            employee__in=employees,
            month=month,
            status=HrPayrollAdjustment.STATUS_APPROVED,
        )
        .values("employee_id", "kind")
        .annotate(total=Sum("amount"))
    )
    result = defaultdict(lambda: defaultdict(Decimal))
    for row in rows:
        result[row["employee_id"]][row["kind"]] += Decimal(row["total"] or 0)
    return result


def _employee_from_actor(actor):
    if isinstance(actor, Employee):
        return actor
    return getattr(actor, "employee_profile", None)


def _productivity_map(start, end):
    filters = {"date_from": start.isoformat(), "date_to": end.isoformat(), "employee": ""}
    result = defaultdict(Decimal)
    for _day, actor, metric, value in warehouse_employee_metric_points(filters):
        if metric not in PRODUCTIVITY_METRICS:
            continue
        employee = _employee_from_actor(actor)
        if employee is not None and employee.pk:
            result[employee.pk] += Decimal(str(value or 0))
    return result


def _rate_for_day(rates, work_date):
    for rate in rates:
        if rate.effective_from <= work_date and (
            rate.effective_to is None or rate.effective_to >= work_date
        ):
            return rate
    return None


def _time_and_rate_calculation(rates, timesheets):
    totals = {
        "planned_minutes": 0,
        "worked_minutes": 0,
        "late_minutes": 0,
        "internship_minutes": 0,
    }
    hourly_accrual = Decimal("0")
    salary_buckets = defaultdict(lambda: {"rate": None, "planned": 0, "worked": 0})
    missing_rate_days = 0
    for row in timesheets:
        for key in totals:
            totals[key] += int(row.get(key) or 0)
        rate = _rate_for_day(rates, row["work_date"])
        if rate is None:
            if row.get("worked_minutes") or row.get("planned_minutes"):
                missing_rate_days += 1
            continue
        worked = Decimal(row.get("worked_minutes") or 0)
        hourly_accrual += Decimal(rate.hourly_rate or 0) * worked / Decimal(60)
        bucket = salary_buckets[rate.pk]
        bucket["rate"] = rate
        bucket["planned"] += int(row.get("planned_minutes") or 0)
        bucket["worked"] += int(row.get("worked_minutes") or 0)

    total_planned = Decimal(totals["planned_minutes"])
    monthly_accrual = Decimal("0")
    if total_planned > 0:
        for bucket in salary_buckets.values():
            rate = bucket["rate"]
            salary = Decimal(rate.monthly_salary or 0)
            planned = Decimal(bucket["planned"])
            worked = Decimal(bucket["worked"])
            if salary and planned > 0:
                period_share = planned / total_planned
                attendance_share = min(worked / planned, Decimal(1))
                monthly_accrual += salary * period_share * attendance_share
    elif totals["worked_minutes"]:
        used_rates = [bucket["rate"] for bucket in salary_buckets.values() if bucket["rate"]]
        latest_used = max(used_rates, key=lambda rate: rate.effective_from, default=None)
        monthly_accrual = Decimal(getattr(latest_used, "monthly_salary", 0) or 0)
    return {
        **totals,
        "hourly_accrual": money(hourly_accrual),
        "monthly_accrual": money(monthly_accrual),
        "missing_rate_days": missing_rate_days,
    }


def _sum_rows(rows):
    keys = (
        "worked_hours",
        "late_hours",
        "internship_hours",
        "accrued",
        "productivity_units",
        "premium",
        "contract_payments",
        "bonuses",
        "deductions",
        "final_due",
    )
    total = {key: Decimal("0") for key in keys}
    for row in rows:
        for key in keys:
            total[key] += Decimal(row.get(key) or 0)
    total["average_hour_cost"] = money(
        total["accrued"] / total["worked_hours"] if total["worked_hours"] else 0
    )
    total["employees"] = len(rows)
    return total


def build_monthly_payroll_report(month: date) -> dict:
    start, end = month_bounds(month)
    employees = list(
        Employee.objects.filter(is_active=True)
        .select_related("hr_profile__department", "user")
        .order_by("full_name", "id")
    )
    rates_by_employee = _rates_by_employee(employees, start, end)
    timesheets_by_employee = _timesheets_by_employee(employees, start, end)
    adjustments = _adjustment_map(employees, month)
    productivity = _productivity_map(start, end)
    rows = []
    for employee in employees:
        profile = getattr(employee, "hr_profile", None)
        employee_rates = rates_by_employee[employee.pk]
        employee_timesheets = timesheets_by_employee[employee.pk]
        time_row = _time_and_rate_calculation(employee_rates, employee_timesheets)
        rate = employee_rates[0] if employee_rates else None
        worked_hours = hours(time_row.get("worked_minutes"))
        hourly_rate = money(getattr(rate, "hourly_rate", 0))
        monthly_salary = money(getattr(rate, "monthly_salary", 0))
        hourly_accrual = time_row["hourly_accrual"]
        monthly_accrual = time_row["monthly_accrual"]
        employee_adjustments = adjustments[employee.pk]
        extra_accrual = money(employee_adjustments[HrPayrollAdjustment.KIND_ACCRUAL])
        accrued = money(hourly_accrual + monthly_accrual + extra_accrual)
        premium = money(employee_adjustments[HrPayrollAdjustment.KIND_PREMIUM])
        contracts = money(employee_adjustments[HrPayrollAdjustment.KIND_CONTRACT_PAYMENT])
        bonuses = money(employee_adjustments[HrPayrollAdjustment.KIND_BONUS])
        deductions = money(employee_adjustments[HrPayrollAdjustment.KIND_DEDUCTION])
        final_due = money(accrued + premium + contracts + bonuses - deductions)
        department = profile.department.name if profile else "Без отдела"
        position = (
            str(profile.position or "").strip()
            if profile
            else ""
        ) or employee.get_role_display()
        has_timesheet = bool(employee_timesheets)
        has_rate = rate is not None
        missing = []
        if not profile:
            missing.append("нет HR-карточки")
        if not has_timesheet:
            missing.append("нет табеля")
        if not has_rate:
            missing.append("нет ставки")
        elif time_row["missing_rate_days"]:
            missing.append("нет ставки на часть табеля")
        rows.append(
            {
                "employee_id": employee.pk,
                "department": department,
                "position": position,
                "employee": employee.full_name,
                "worked_hours": worked_hours,
                "late_hours": hours(time_row.get("late_minutes")),
                "internship_hours": hours(time_row.get("internship_minutes")),
                "hourly_rate": hourly_rate,
                "monthly_salary": monthly_salary,
                "accrued": accrued,
                "productivity_units": productivity[employee.pk].quantize(HOUR_STEP),
                "premium": premium,
                "average_hour_cost": money(accrued / worked_hours if worked_hours else 0),
                "contract_payments": contracts,
                "bonuses": bonuses,
                "deductions": deductions,
                "final_due": final_due,
                "source_status": ", ".join(missing) if missing else "данные заполнены",
                "has_source_gap": bool(missing),
            }
        )
    rows.sort(key=lambda row: (row["department"].casefold(), row["employee"].casefold()))
    departments = []
    for department_name in sorted({row["department"] for row in rows}, key=str.casefold):
        department_rows = [row for row in rows if row["department"] == department_name]
        departments.append(
            {"name": department_name, "rows": department_rows, "totals": _sum_rows(department_rows)}
        )
    totals = _sum_rows(rows)
    totals["source_gaps"] = sum(1 for row in rows if row["has_source_gap"])
    return {
        "month": month,
        "month_value": month.strftime("%Y-%m"),
        "month_label": month.strftime("%m.%Y"),
        "start": start,
        "end": end,
        "rows": rows,
        "departments": departments,
        "totals": totals,
    }
