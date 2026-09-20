from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("logistics", "0008_driver_trip_workflow"),
    ]

    operations = [
        migrations.AlterField(
            model_name="logisticstrip",
            name="driver_status",
            field=models.CharField(
                choices=[
                    ("unassigned", "Водитель не назначен"),
                    ("preliminary", "Предварительный рейс"),
                    ("assigned", "Водитель назначен"),
                    ("accepted", "Рейс принят водителем"),
                    ("en_route", "Водитель выехал"),
                    ("awaiting_delivery", "Ожидает сдачи"),
                    ("delivered", "Груз сдан"),
                    ("problem", "Проблемный рейс"),
                ],
                db_index=True,
                default="unassigned",
                max_length=32,
                verbose_name="Статус водителя",
            ),
        ),
    ]
