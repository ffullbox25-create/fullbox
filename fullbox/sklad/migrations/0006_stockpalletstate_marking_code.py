from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sklad", "0005_stockpalletstate_reserved_available_qty"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockpalletstate",
            name="marking_code",
            field=models.CharField(blank=True, max_length=128),
        ),
    ]
