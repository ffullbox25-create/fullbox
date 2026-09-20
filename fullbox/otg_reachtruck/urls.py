from django.urls import path

from .views import OtgReachtruckDashboardView


app_name = "otg_reachtruck"

urlpatterns = [
    path("", OtgReachtruckDashboardView.as_view(), name="dashboard"),
]
