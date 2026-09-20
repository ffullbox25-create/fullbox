from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0041_alter_fbspickrestockscan_stage"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fbspickrestockscan",
            name="stage",
            field=models.CharField(
                choices=[
                    ("workstation", "Исходный рабочий стол"),
                    ("pickup_item", "Снятие товара со стола"),
                    ("cell", "Исходная ячейка"),
                    ("box", "Исходный короб"),
                    ("item", "Возврат товара"),
                ],
                max_length=16,
            ),
        ),
    ]
