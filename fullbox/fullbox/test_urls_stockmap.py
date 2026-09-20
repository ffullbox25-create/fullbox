from django.urls import include, path


urlpatterns = [
    path("stockmap/", include("stockmap.urls")),
]
