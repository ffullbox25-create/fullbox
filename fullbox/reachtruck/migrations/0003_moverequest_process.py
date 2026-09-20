"""Add MoveRequest.process.

Django populates existing rows from ``default`` and then drops the database
default, which would leave the column NOT NULL with no default.  During a
deploy the old code is still running and inserts a MoveRequest without this
column, so that window would raise a NOT NULL violation on live warehouse work.
The RunSQL below keeps a database-level default so old and new code can both
write while the deploy is in flight.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reachtruck", "0002_boxclaim_palletlock"),
    ]

    operations = [
        migrations.AddField(
            model_name="moverequest",
            name="process",
            field=models.CharField(
                blank=True,
                choices=[
                    ("shipping", "Отгрузка"),
                    ("fbs", "Перемещение ФБС"),
                    ("processing", "Обработка"),
                    ("receiving", "Приёмка"),
                    ("manual", "Ручной запрос"),
                ],
                db_index=True,
                default="",
                max_length=20,
            ),
        ),
        migrations.RunSQL(
            sql="ALTER TABLE reachtruck_moverequest ALTER COLUMN process SET DEFAULT ''",
            reverse_sql="ALTER TABLE reachtruck_moverequest ALTER COLUMN process DROP DEFAULT",
        ),
    ]
