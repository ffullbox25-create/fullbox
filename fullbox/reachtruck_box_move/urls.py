from django.urls import path

from .views import ReachtruckBoxMoveDashboardView


app_name = "reachtruck_box_move"

urlpatterns = [
    path("", ReachtruckBoxMoveDashboardView.as_view(), name="dashboard"),
]
