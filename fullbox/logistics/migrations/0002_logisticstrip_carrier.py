from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("head_manager", "0003_carrier_bank_address_carrier_bank_bik_and_more"),
        ("logistics", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="logisticstrip",
            name="carrier",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="logistics_trips",
                to="head_manager.carrier",
                verbose_name="Перевозчик",
            ),
        ),
    ]
