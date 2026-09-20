from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = "hr"

urlpatterns = [
    path("", views.HrDashboardView.as_view(), name="dashboard"),
    path(
        "payroll/departments/",
        views.HrPayrollDepartmentsView.as_view(),
        name="payroll-departments",
    ),
    path(
        "payroll/employees/",
        views.HrPayrollEmployeesView.as_view(),
        name="payroll-employees",
    ),
    path(
        "payroll/employees/<int:employee_id>/",
        views.HrPayrollEmployeeDetailView.as_view(),
        name="payroll-employee-detail",
    ),
    path(
        "payroll/timesheet/",
        views.HrPayrollTimesheetView.as_view(),
        name="payroll-timesheet",
    ),
    path(
        "payroll/adjustments/",
        views.HrPayrollAdjustmentsView.as_view(),
        name="payroll-adjustments",
    ),
    path(
        "payroll/adjustments/<int:adjustment_id>/approve/",
        views.HrPayrollAdjustmentApproveView.as_view(),
        name="payroll-adjustment-approve",
    ),
    path(
        "reports/monthly/",
        views.HrMonthlyPayrollReportView.as_view(),
        name="monthly-payroll-report",
    ),
    path(
        "reports/monthly/export/",
        views.HrMonthlyPayrollExportView.as_view(),
        name="monthly-payroll-export",
    ),
    path(
        "reports/payroll-audit/",
        views.HrPayrollAuditView.as_view(),
        name="payroll-audit",
    ),
    path("integrations/", views.HrIntegrationsView.as_view(), name="integrations"),
    path(
        "integrations/<slug:integration_key>/",
        views.HrIntegrationDetailView.as_view(),
        name="integration-detail",
    ),
    path("employees/", RedirectView.as_view(url="/employees/", permanent=False), name="employees"),
]
