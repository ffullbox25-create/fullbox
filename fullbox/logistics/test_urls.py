from django.urls import include, path


urlpatterns = [path("logistics/", include("logistics.urls"))]
