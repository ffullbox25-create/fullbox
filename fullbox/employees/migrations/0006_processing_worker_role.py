from django.db import migrations, models


def create_processing_workers(apps, schema_editor):
    Employee = apps.get_model("employees", "Employee")
    for name in ("Обработчик 1", "Обработчик 2"):
        Employee.objects.get_or_create(
            full_name=name,
            role="processing_worker",
            defaults={"is_active": True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0005_alter_employee_role"),
    ]

    operations = [
        migrations.AlterField(
            model_name="employee",
            name="role",
            field=models.CharField(
                choices=[
                    ("admin", "Администратор"),
                    ("director", "Директор"),
                    ("accountant", "Бухгалтер"),
                    ("head_manager", "Главный менеджер"),
                    ("processing_head", "Руководитель участка обработки"),
                    ("processing_worker", "Обработчик"),
                    ("manager", "Менеджер"),
                    ("storekeeper", "Кладовщик"),
                    ("reachtruck_driver", "Водитель ричтрака"),
                    ("picker", "Сборщик"),
                    ("developer", "Разработчик"),
                ],
                max_length=32,
            ),
        ),
        migrations.RunPython(create_processing_workers, migrations.RunPython.noop),
    ]
