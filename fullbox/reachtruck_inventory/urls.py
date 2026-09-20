from django.urls import path

from .views import ReachtruckInventoryDashboardView, ReachtruckInventoryTaskView


app_name = "reachtruck_inventory"

urlpatterns = [
    path("", ReachtruckInventoryDashboardView.as_view(), name="dashboard"),
    path("<int:pk>/", ReachtruckInventoryTaskView.as_view(), name="task"),
]

