from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("fbs", "0009_fbsordertraceability_fbsmarketplacemetadatatransfer_and_more")]

    operations = [
        migrations.AlterField(
            model_name="fbsmarketplacemetadatatransfer",
            name="status",
            field=models.CharField(
                choices=[
                    ("prepared", "Подготовлено"),
                    ("queued", "В очереди"),
                    ("sent", "Отправлено"),
                    ("confirmed", "Подтверждено"),
                    ("retry", "Повтор"),
                    ("failed", "Ошибка"),
                    ("conflict", "Конфликт"),
                    ("unsupported", "Не поддерживается API"),
                    ("canceled", "Отменено"),
                ],
                default="prepared",
                max_length=16,
            ),
        )
    ]
