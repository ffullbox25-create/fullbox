from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sklad", "0011_live_snapshot_query_indexes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="warehousereserve",
            name="reserve_type",
            field=models.CharField(
                choices=[
                    ("processing", "Под обработку"),
                    ("shipping", "Под отгрузку"),
                    ("fbs_movement", "Под перемещение в FBS"),
                    ("manual", "Ручной"),
                ],
                max_length=32,
            ),
        ),
    ]
