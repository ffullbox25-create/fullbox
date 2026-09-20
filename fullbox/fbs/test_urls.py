from django.contrib.auth import get_user_model, login
from django.http import HttpResponseNotFound
from django.shortcuts import redirect
from django.urls import include, path


def visual_sign_in(request):
    user = get_user_model().objects.filter(username="fbs-operator-preview", is_active=True).first()
    if user is None:
        return HttpResponseNotFound("Preview user is not configured.")
    login(request, user)
    return redirect("/fbs/operator/movements/")


def visual_driver_sign_in(request):
    user = get_user_model().objects.filter(
        username="fbs-reachtruck-preview", is_active=True
    ).first()
    if user is None:
        return HttpResponseNotFound("Preview driver is not configured.")
    login(request, user)
    return redirect("/reachtruck/")


urlpatterns = [
    path("__test-login__/", visual_sign_in),
    path("__test-driver-login__/", visual_driver_sign_in),
    path("fbs/", include("fbs.urls")),
    path("reachtruck/", include("reachtruck.urls")),
    path("reachtruck-box-move/", include("reachtruck_box_move.urls")),
]
