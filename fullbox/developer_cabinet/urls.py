from django.urls import path

from . import views


app_name = "developer_cabinet"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("chz-import/", views.chz_import, name="chz_import"),
]
