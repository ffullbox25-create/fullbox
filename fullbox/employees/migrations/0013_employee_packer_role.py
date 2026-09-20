from django.db import migrations, models


PACKER_EMPLOYEES = {
    31: "Костина Ольга",
    32: "Кирьяк Анастасия",
    33: "Цыганова Ирина",
    34: "Барбарова Диана",
    37: "Григорян Шушик",
}


def assign_packer_role(apps, schema_editor):
    Employee = apps.get_model("employees", "Employee")
    for employee_id, full_name in PACKER_EMPLOYEES.items():
        Employee.objects.filter(
            id=employee_id,
            full_name=full_name,
            role="processing_worker",
        ).update(role="packer")


def restore_processing_worker_role(apps, schema_editor):
    Employee = apps.get_model("employees", "Employee")
    for employee_id, full_name in PACKER_EMPLOYEES.items():
        Employee.objects.filter(
            id=employee_id,
            full_name=full_name,
            role="packer",
        ).update(role="processing_worker")


class Migration(migrations.Migration):
    dependencies = [
        ("employees", "0012_employee_access_roles"),
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
                    ("hr", "Отдел кадров"),
                    ("head_manager", "Главный менеджер"),
                    ("processing_head", "Руководитель участка обработки"),
                    ("packer", "Упаковщица"),
                    ("processing_worker", "Обработчик"),
                    ("manager", "Менеджер"),
                    ("storekeeper", "Кладовщик"),
                    ("logistician", "Логист"),
                    ("driver", "Водитель"),
                    ("reachtruck_driver", "Водитель ричтрака"),
                    ("super_car", "Суперкар"),
                    ("picker", "Сборщик"),
                    ("developer", "Разработчик"),
                ],
                max_length=32,
                verbose_name="Роль",
            ),
        ),
        migrations.RunPython(
            assign_packer_role,
            restore_processing_worker_role,
        ),
    ]
