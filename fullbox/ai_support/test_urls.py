from django.urls import include, path


urlpatterns = [path("ai-support/", include("ai_support.urls"))]
