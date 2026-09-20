from django.contrib.auth import get_user_model, login
from django.shortcuts import redirect
from django.urls import include, path

from employees.models import Employee


def local_storekeeper_login(request):
    user, _ = get_user_model().objects.get_or_create(username="local_storekeeper_kb")
    user.is_active = True
    user.set_unusable_password()
    user.save(update_fields=["password", "is_active"])
    Employee.objects.update_or_create(
        user=user,
        defaults={
            "full_name": "Оператор FBS",
            "role": "storekeeper",
            "is_active": True,
        },
    )
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("/sklad/knowledge/fbs-storekeeper-operator/")


urlpatterns = [
    path("__test_login__/", local_storekeeper_login),
    path("sklad/", include("sklad.urls")),
    path("todo/", include("todo.urls")),
]
