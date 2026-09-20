from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("employees", "0011_employee_driver_role"),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="access_roles",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Дополнительные кабинеты и полномочия без изменения основной роли сотрудника.",
                verbose_name="Дополнительные роли",
            ),
        ),
    ]
