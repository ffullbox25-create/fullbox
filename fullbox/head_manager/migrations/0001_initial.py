from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Carrier",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, verbose_name="Полное название")),
                ("short_name", models.CharField(blank=True, max_length=255, verbose_name="Сокращенное название")),
                ("inn", models.CharField(blank=True, max_length=32, verbose_name="ИНН")),
                ("kpp", models.CharField(blank=True, max_length=32, verbose_name="КПП")),
                ("ogrn", models.CharField(blank=True, max_length=32, verbose_name="ОГРН / ОГРНИП")),
                ("address", models.TextField(blank=True, verbose_name="Адрес")),
                ("phone", models.CharField(blank=True, max_length=32, verbose_name="Телефон")),
                ("email", models.EmailField(blank=True, max_length=254, verbose_name="Email")),
                ("contact_person", models.CharField(blank=True, max_length=255, verbose_name="Контактное лицо")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активен")),
                ("comment", models.TextField(blank=True, verbose_name="Комментарий")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлено")),
            ],
            options={
                "verbose_name": "Перевозчик",
                "verbose_name_plural": "Перевозчики",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="OwnCompany",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, verbose_name="Полное название")),
                ("short_name", models.CharField(blank=True, max_length=255, verbose_name="Сокращенное название")),
                ("inn", models.CharField(blank=True, max_length=32, verbose_name="ИНН")),
                ("kpp", models.CharField(blank=True, max_length=32, verbose_name="КПП")),
                ("ogrn", models.CharField(blank=True, max_length=32, verbose_name="ОГРН / ОГРНИП")),
                ("address", models.TextField(blank=True, verbose_name="Адрес")),
                ("phone", models.CharField(blank=True, max_length=32, verbose_name="Телефон")),
                ("email", models.EmailField(blank=True, max_length=254, verbose_name="Email")),
                ("is_default", models.BooleanField(default=False, verbose_name="Компания по умолчанию")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активна")),
                ("comment", models.TextField(blank=True, verbose_name="Комментарий")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлено")),
            ],
            options={
                "verbose_name": "Наша компания",
                "verbose_name_plural": "Наши компании",
                "ordering": ["-is_default", "name"],
            },
        ),
    ]
