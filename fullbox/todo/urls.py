from django.urls import path

from . import views
from .request_voice_alerts import request_voice_alerts

app_name = "todo"

urlpatterns = [
    path("", views.task_list, name="list"),
    path("api/request-voice-alerts/", request_voice_alerts, name="request_voice_alerts"),
    path("panel/chunk/", views.task_panel_chunk, name="panel_chunk"),
    path("new/", views.task_create, name="create"),
    path("<int:pk>/open/", views.task_open, name="open"),
    path("<int:pk>/", views.task_detail, name="detail"),
    path("<int:pk>/edit/", views.task_update, name="update"),
    path("<int:pk>/delete/", views.task_delete, name="delete"),
]
