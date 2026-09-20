from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accountant", "0002_activate_existing_clients"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clientlifecycle",
            name="client_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ip", "ИП"),
                    ("ooo", "ООО"),
                    ("ao", "Акционерное общество"),
                    ("self_employed", "Самозанятый"),
                    ("person", "Физлицо"),
                ],
                max_length=32,
                verbose_name="Тип клиента",
            ),
        ),
    ]
