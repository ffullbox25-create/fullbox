from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("receiving_distribution", "0001_initial_distribution_plan"),
    ]

    operations = [
        migrations.AddField(
            model_name="receivingdistributionallocation",
            name="qty_accepted",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Заполняется хуком из текущей приёмки; не является отдельным складским регистром",
                verbose_name="Фактически принято по направлению",
            ),
        ),
    ]
