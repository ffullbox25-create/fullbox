from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import migrations


def ensure_dev_login(apps, schema_editor):
    auth_app, auth_model = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(auth_app, auth_model)
    Employee = apps.get_model("employees", "Employee")

    user, _ = User.objects.get_or_create(
        username="dev",
        defaults={
            "is_active": True,
            "is_staff": False,
            "is_superuser": False,
        },
    )
    user.password = make_password("123")
    user.is_active = True
    user.save(update_fields=["password", "is_active"])

    employee = Employee.objects.filter(user=user).first()
    if employee is None:
        employee = Employee.objects.filter(role="developer").order_by("id").first()

    if employee is None:
        Employee.objects.create(
            full_name="Developer",
            user=user,
            role="developer",
            is_active=True,
        )
        return

    employee.user = user
    employee.role = "developer"
    employee.is_active = True
    if not employee.full_name:
        employee.full_name = "Developer"
    employee.save(update_fields=["user", "role", "is_active", "full_name"])


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("employees", "0009_super_car_role_and_user"),
    ]

    operations = [
        migrations.RunPython(ensure_dev_login, migrations.RunPython.noop),
    ]
