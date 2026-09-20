from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("client_cabinet", "0008_chat_mentions_pins_audit"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatnotificationpreference",
            name="telegram_chat_id",
            field=models.CharField(
                blank=True, default="", max_length=64, verbose_name="Telegram chat id"
            ),
        ),
        migrations.AddField(
            model_name="chatnotificationpreference",
            name="telegram_enabled",
            field=models.BooleanField(default=False, verbose_name="Telegram включён"),
        ),
        migrations.AddField(
            model_name="chatnotificationpreference",
            name="telegram_link_code",
            field=models.CharField(
                blank=True, default="", max_length=32, verbose_name="Код привязки Telegram"
            ),
        ),
        migrations.AddField(
            model_name="chatnotificationpreference",
            name="telegram_link_expires_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Код привязки действует до"
            ),
        ),
    ]
