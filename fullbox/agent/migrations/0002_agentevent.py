from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("agent", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("agent_id", models.CharField(db_index=True, max_length=64, verbose_name="ID агента")),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("scan", "Скан"),
                            ("print", "Печать"),
                            ("status", "Статус"),
                            ("error", "Ошибка"),
                        ],
                        max_length=16,
                        verbose_name="Тип события",
                    ),
                ),
                ("payload", models.JSONField(blank=True, default=dict, verbose_name="Данные")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Событие агента",
                "verbose_name_plural": "События агента",
                "ordering": ["-created_at"],
            },
        ),
    ]
