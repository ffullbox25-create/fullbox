from django.urls import path

from .reports import (
    ProcessingClosedDiscrepancyCorrectionTaskView,
    ProcessingClosedDiscrepancyReviewView,
    ProcessingHeadReportsView,
)
from .views import ProcessingHeadDashboard, ProcessingHeadRequestsView

urlpatterns = [
    path("", ProcessingHeadDashboard.as_view(), name="processing-head-dashboard"),
    path("requests/", ProcessingHeadRequestsView.as_view(), name="processing-head-requests"),
    path("reports", ProcessingHeadReportsView.as_view(), name="processing-head-reports"),
    path(
        "reports/closed-discrepancies/review/<str:order_id>",
        ProcessingClosedDiscrepancyReviewView.as_view(),
        name="processing-head-closed-discrepancy-review",
    ),
    path(
        "reports/closed-discrepancies/correction-task/<str:order_id>",
        ProcessingClosedDiscrepancyCorrectionTaskView.as_view(),
        name="processing-head-closed-discrepancy-correction-task",
    ),
    path("reports/<slug:slug>", ProcessingHeadReportsView.as_view(), name="processing-head-report-detail"),
]
