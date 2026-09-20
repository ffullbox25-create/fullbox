from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0040_controller_tote_lifecycle"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fbspickrestockscan",
            name="stage",
            field=models.CharField(
                choices=[
                    ("workstation", "Исходный рабочий стол"),
                    ("cell", "Исходная ячейка"),
                    ("box", "Исходный короб"),
                    ("item", "Возврат товара"),
                ],
                max_length=16,
            ),
        ),
    ]
