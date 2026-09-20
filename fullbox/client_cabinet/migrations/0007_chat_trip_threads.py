from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("client_cabinet", "0006_chat_messenger_upgrade"),
    ]

    operations = [
        migrations.AlterField(
            model_name="chatthread",
            name="kind",
            field=models.CharField(
                choices=[
                    ("client_general", "Клиент — FullBox"),
                    ("order_client", "Чат заявки (клиент)"),
                    ("order_internal", "Внутренний чат заявки"),
                    ("task", "Чат задачи"),
                    ("trip", "Чат рейса"),
                    ("manual", "Ручной чат"),
                ],
                db_index=True,
                max_length=32,
                verbose_name="Тип",
            ),
        ),
        migrations.AddConstraint(
            model_name="chatthread",
            constraint=models.UniqueConstraint(
                condition=models.Q(kind="trip") & ~models.Q(order_id=""),
                fields=("kind", "order_type", "order_id"),
                name="uniq_chat_thread_trip",
            ),
        ),
    ]
