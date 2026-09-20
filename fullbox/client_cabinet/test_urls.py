from django.contrib.auth import get_user_model, login
from django.http import HttpResponseNotFound
from django.shortcuts import redirect
from django.urls import include, path


def visual_sign_in(request):
    user = get_user_model().objects.filter(username="fbs-preview", is_active=True).first()
    if user is None:
        return HttpResponseNotFound("Preview user is not configured.")
    login(request, user)
    return redirect("/client/dashboard/lk/#/fbs")


urlpatterns = [
    path("__test-login__/", visual_sign_in),
    path("client/", include("client_cabinet.urls")),
]
