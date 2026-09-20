from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="DeviceAgent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("agent_id", models.CharField(max_length=64, unique=True, verbose_name="ID агента")),
                ("name", models.CharField(blank=True, max_length=128, verbose_name="Имя")),
                ("host", models.CharField(blank=True, max_length=128, verbose_name="Хост")),
                ("version", models.CharField(blank=True, max_length=32, verbose_name="Версия")),
                ("last_ip", models.GenericIPAddressField(blank=True, null=True, verbose_name="IP")),
                ("last_seen", models.DateTimeField(blank=True, null=True, verbose_name="Последняя активность")),
                ("meta", models.JSONField(blank=True, default=dict, verbose_name="Метаданные")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Агент",
                "verbose_name_plural": "Агенты",
                "ordering": ["-last_seen", "-updated_at"],
            },
        ),
        migrations.CreateModel(
            name="AgentCommand",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("agent_id", models.CharField(blank=True, db_index=True, max_length=64, verbose_name="ID агента")),
                ("command", models.CharField(max_length=64, verbose_name="Команда")),
                ("payload", models.JSONField(blank=True, default=dict, verbose_name="Параметры")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидает"),
                            ("delivered", "Доставлено"),
                            ("done", "Выполнено"),
                            ("failed", "Ошибка"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("delivered_at", models.DateTimeField(blank=True, null=True, verbose_name="Доставлено")),
                ("acked_at", models.DateTimeField(blank=True, null=True, verbose_name="Подтверждено")),
                ("result", models.JSONField(blank=True, default=dict, verbose_name="Результат")),
                ("error", models.TextField(blank=True, verbose_name="Ошибка")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Команда агента",
                "verbose_name_plural": "Команды агента",
                "ordering": ["-created_at"],
            },
        ),
    ]
