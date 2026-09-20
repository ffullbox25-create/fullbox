from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("logistics", "0005_trip_edit_version"),
    ]

    operations = [
        migrations.AlterField(
            model_name="shippingroutingstate",
            name="status",
            field=models.CharField(
                choices=[
                    ("ready_for_routing", "Готова к маршрутизации"),
                    ("needs_clarification", "Требуется уточнение"),
                    ("routed", "Рейс назначен"),
                    ("in_transit", "В пути"),
                    ("delivered", "Доставка завершена"),
                    ("delivery_failed", "Не сдана на МП"),
                ],
                db_index=True,
                default="ready_for_routing",
                max_length=32,
            ),
        ),
    ]
