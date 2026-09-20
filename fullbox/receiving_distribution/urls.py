from django.urls import path

from . import views

app_name = "receiving_distribution"

urlpatterns = [
    path(
        "<int:pk>/receiving/choose/",
        views.receiving_type_choice,
        name="receiving-type-choice",
    ),
    path(
        "<int:pk>/receiving/distribution/new/",
        views.distribution_create,
        name="distribution-create",
    ),
    path(
        "<int:pk>/receiving/distribution/template.xlsx",
        views.distribution_template_download,
        name="distribution-template",
    ),
    path(
        "<int:pk>/receiving/distribution/check/",
        views.distribution_check,
        name="distribution-check",
    ),
    path(
        "<int:pk>/receiving/distribution/error-report/",
        views.distribution_error_report,
        name="distribution-error-report",
    ),
]
