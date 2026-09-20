from django.urls import path

from .views import ProcessingWorkerDashboard

app_name = "processing_worker"

urlpatterns = [
    path("", ProcessingWorkerDashboard.as_view(), name="processing-worker-dashboard"),
]
