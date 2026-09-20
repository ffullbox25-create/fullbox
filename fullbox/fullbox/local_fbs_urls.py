from django.shortcuts import redirect
from django.urls import include, path

from fullbox import views as fullbox_views


def local_fbs_sign_in(request):
    response = fullbox_views.sign_in(request)
    if response.status_code in {301, 302}:
        destination = response.get("Location")
        if destination == "/head-manager/":
            response["Location"] = "/head-manager/fbs/"
        elif destination == "/sklad/":
            response["Location"] = "/fbs/tsd/storekeeper/"
    return response


urlpatterns = [
    path("", lambda request: redirect("login")),
    path("login/", local_fbs_sign_in, name="login"),
    path("logout/", fullbox_views.sign_out, name="logout"),
    path("head-manager/", lambda request: redirect("head-manager-fbs")),
    path("head-manager/", include("head_manager.urls")),
    path("fbs/", include("fbs.urls")),
]
