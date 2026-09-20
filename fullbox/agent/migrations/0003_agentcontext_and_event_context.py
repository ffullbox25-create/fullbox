from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("agent", "0002_agentevent"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentContext",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("agent_id", models.CharField(db_index=True, max_length=64, verbose_name="ID агента")),
                ("context_id", models.CharField(max_length=64, unique=True, verbose_name="Контекст")),
                ("role", models.CharField(blank=True, max_length=32, verbose_name="Роль")),
                ("order_id", models.IntegerField(blank=True, db_index=True, null=True, verbose_name="Заявка")),
                ("box_id", models.CharField(blank=True, max_length=64, verbose_name="Короб")),
                ("session_key", models.CharField(blank=True, max_length=64, verbose_name="Сессия")),
                ("active", models.BooleanField(default=True, verbose_name="Активен")),
                ("last_seen", models.DateTimeField(blank=True, null=True, verbose_name="Последняя активность")),
                ("expires_at", models.DateTimeField(blank=True, null=True, verbose_name="Истекает")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="agent_contexts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Контекст агента",
                "verbose_name_plural": "Контексты агента",
                "ordering": ["-updated_at"],
            },
        ),
        migrations.AddField(
            model_name="agentevent",
            name="context_box_id",
            field=models.CharField(blank=True, max_length=64, verbose_name="Короб"),
        ),
        migrations.AddField(
            model_name="agentevent",
            name="context_id",
            field=models.CharField(blank=True, db_index=True, max_length=64, verbose_name="Контекст"),
        ),
        migrations.AddField(
            model_name="agentevent",
            name="context_order_id",
            field=models.IntegerField(blank=True, db_index=True, null=True, verbose_name="Заявка"),
        ),
        migrations.AddField(
            model_name="agentevent",
            name="context_role",
            field=models.CharField(blank=True, max_length=32, verbose_name="Роль"),
        ),
        migrations.AddField(
            model_name="agentevent",
            name="context_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="agent_events",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
