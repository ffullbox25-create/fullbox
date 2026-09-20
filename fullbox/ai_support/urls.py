from django.urls import path

from . import views


app_name = "ai_support"

urlpatterns = [
    path("", views.incident_list, name="list"),
    path("new/", views.incident_create, name="create"),
    path("<int:pk>/", views.incident_detail, name="detail"),
    path("<int:pk>/message/", views.incident_message, name="message"),
    path("<int:pk>/status/", views.incident_status, name="status"),
]
