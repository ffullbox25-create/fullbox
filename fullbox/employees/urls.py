from django.urls import path

from . import views

app_name = "employees"

urlpatterns = [
    path("", views.employee_list, name="list"),
    path("export-logins/", views.employee_export_logins, name="export-logins"),
    path("<int:pk>/facsimile/", views.employee_facsimile, name="facsimile"),
    path("new/", views.EmployeeCreateView.as_view(), name="create"),
    path("<int:pk>/enter/", views.employee_enter_cabinet, name="enter"),
    path("<int:pk>/edit/", views.EmployeeEditView.as_view(), name="edit"),
    path("<int:pk>/badge/issue/", views.employee_badge_issue, name="badge-issue"),
    path("<int:pk>/badge/toggle/", views.employee_badge_toggle, name="badge-toggle"),
    path("<int:pk>/badge/revoke/", views.employee_badge_revoke, name="badge-revoke"),
]
