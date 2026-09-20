from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0016_billing_staff_notification"),
    ]

    operations = [
        migrations.AddField(
            model_name="applicationcharge",
            name="previous_service_name",
            field=models.CharField(blank=True, max_length=255, verbose_name="Предыдущая услуга"),
        ),
        migrations.AddField(
            model_name="applicationcharge",
            name="service_changed_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="Услуга изменена менеджером",
            ),
        ),
    ]
