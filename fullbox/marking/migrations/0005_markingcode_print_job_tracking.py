from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marking", "0004_markingcode_printed_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="markingcode",
            name="print_job_id",
            field=models.PositiveBigIntegerField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="Задание печати",
            ),
        ),
        migrations.AddField(
            model_name="markingcode",
            name="print_reserved_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Передан в очередь печати",
            ),
        ),
    ]
