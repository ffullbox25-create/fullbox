import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("client_cabinet", "0009_chat_telegram_prefs"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ChatTelegramBotConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("bot_token", models.CharField(blank=True, default="", max_length=256, verbose_name="Токен бота")),
                (
                    "bot_username",
                    models.CharField(blank=True, default="", max_length=128, verbose_name="Username бота"),
                ),
                (
                    "webhook_secret",
                    models.CharField(blank=True, default="", max_length=256, verbose_name="Secret webhook"),
                ),
                (
                    "alert_chat_id",
                    models.CharField(blank=True, default="", max_length=64, verbose_name="Общий alert chat id"),
                ),
                (
                    "lk_base_url",
                    models.CharField(blank=True, default="", max_length=255, verbose_name="Базовый URL ЛК"),
                ),
                ("is_enabled", models.BooleanField(default=False, verbose_name="Включено")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="updated_chat_telegram_configs",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто обновил",
                    ),
                ),
            ],
            options={
                "verbose_name": "Настройки Telegram-бота чатов",
                "verbose_name_plural": "Настройки Telegram-бота чатов",
            },
        ),
    ]
