from django.urls import include, path

from fullbox import views as fullbox_views


def local_head_manager_sign_in(request):
    response = fullbox_views.sign_in(request)
    if response.status_code in {301, 302} and response.get("Location") == "/head-manager/":
        response["Location"] = "/head-manager/fbs/"
    return response


urlpatterns = [
    path("login/", local_head_manager_sign_in, name="login"),
    path("logout/", fullbox_views.sign_out, name="logout"),
    path("head-manager/", include("head_manager.urls")),
]
