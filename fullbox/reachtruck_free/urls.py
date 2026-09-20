from django.urls import path

from .views import ReachtruckFreeDashboardView


app_name = "reachtruck_free"

urlpatterns = [
    path("", ReachtruckFreeDashboardView.as_view(), name="dashboard"),
]
