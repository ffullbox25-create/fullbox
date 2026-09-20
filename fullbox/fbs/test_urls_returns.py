from django.urls import include, path


urlpatterns = [path("fbs/", include("fbs.urls"))]
