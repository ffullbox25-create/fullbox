from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0007_processing_worker_names"),
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
                    ("logistician", "Логист"),
                    ("reachtruck_driver", "Водитель ричтрака"),
                    ("picker", "Сборщик"),
                    ("developer", "Разработчик"),
                ],
                max_length=32,
                verbose_name="Роль",
            ),
        ),
    ]
