from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import migrations, models


SUPER_IVAN_USERNAME = "super_ivan"
SUPER_IVAN_NAME = "\u0421\u0443\u043f\u0435\u0440 \u0418\u0432\u0430\u043d"


def ensure_super_ivan(apps, schema_editor):
    Employee = apps.get_model("employees", "Employee")
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)

    user, created = User.objects.get_or_create(
        username=SUPER_IVAN_USERNAME,
        defaults={"is_active": True},
    )
    if created:
        user.password = make_password(None)
        user.save(update_fields=["password"])
    elif hasattr(user, "is_active") and not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])

    employee = Employee.objects.filter(user=user).order_by("id").first()
    if employee is None:
        employee = Employee.objects.filter(full_name=SUPER_IVAN_NAME).order_by("id").first()

    if employee is None:
        Employee.objects.create(
            full_name=SUPER_IVAN_NAME,
            role="super_car",
            is_active=True,
            user=user,
        )
        return

    update_fields = []
    if employee.full_name != SUPER_IVAN_NAME:
        employee.full_name = SUPER_IVAN_NAME
        update_fields.append("full_name")
    if employee.role != "super_car":
        employee.role = "super_car"
        update_fields.append("role")
    if not employee.is_active:
        employee.is_active = True
        update_fields.append("is_active")
    if employee.user_id != user.id:
        employee.user = user
        update_fields.append("user")
    if update_fields:
        employee.save(update_fields=update_fields)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("employees", "0008_employee_logistician_role"),
    ]

    operations = [
        migrations.AlterField(
            model_name="employee",
            name="role",
            field=models.CharField(
                choices=[
                    ("admin", "\u0410\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440"),
                    ("director", "\u0414\u0438\u0440\u0435\u043a\u0442\u043e\u0440"),
                    ("accountant", "\u0411\u0443\u0445\u0433\u0430\u043b\u0442\u0435\u0440"),
                    ("head_manager", "\u0413\u043b\u0430\u0432\u043d\u044b\u0439 \u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440"),
                    ("processing_head", "\u0420\u0443\u043a\u043e\u0432\u043e\u0434\u0438\u0442\u0435\u043b\u044c \u0443\u0447\u0430\u0441\u0442\u043a\u0430 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438"),
                    ("processing_worker", "\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a"),
                    ("manager", "\u041c\u0435\u043d\u0435\u0434\u0436\u0435\u0440"),
                    ("storekeeper", "\u041a\u043b\u0430\u0434\u043e\u0432\u0449\u0438\u043a"),
                    ("logistician", "\u041b\u043e\u0433\u0438\u0441\u0442"),
                    ("reachtruck_driver", "\u0412\u043e\u0434\u0438\u0442\u0435\u043b\u044c \u0440\u0438\u0447\u0442\u0440\u0430\u043a\u0430"),
                    ("super_car", "\u0421\u0443\u043f\u0435\u0440\u043a\u0430\u0440"),
                    ("picker", "\u0421\u0431\u043e\u0440\u0449\u0438\u043a"),
                    ("developer", "\u0420\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a"),
                ],
                max_length=32,
                verbose_name="\u0420\u043e\u043b\u044c",
            ),
        ),
        migrations.RunPython(ensure_super_ivan, migrations.RunPython.noop),
    ]
