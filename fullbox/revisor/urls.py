from django.urls import path

from .views import RevisorIndexView, RevisorProductCheckView


app_name = "revisor"

urlpatterns = [
    path("", RevisorIndexView.as_view(), name="index"),
    path("product-check/", RevisorProductCheckView.as_view(), name="product-check"),
]
