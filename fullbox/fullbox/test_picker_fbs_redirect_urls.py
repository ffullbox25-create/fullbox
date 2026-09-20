from django.urls import path
from django.views.generic import RedirectView

from fullbox.views import role_cabinet, sign_in


urlpatterns = [
    path("login/", sign_in, name="login"),
    path(
        "cabinet/picker/",
        RedirectView.as_view(url="/fbs/tsd/picking/", permanent=False),
        name="picker-cabinet",
    ),
    path("cabinet/<str:role>/", role_cabinet, name="role-cabinet"),
]
