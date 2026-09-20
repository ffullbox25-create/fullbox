import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("client_cabinet", "0010_chat_telegram_bot_config"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ChatAISettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("copilot_enabled", models.BooleanField(default=True, verbose_name="Copilot менеджера")),
                ("auto_reply_enabled", models.BooleanField(default=False, verbose_name="Автоответы клиенту")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="updated_chat_ai_settings",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто обновил",
                    ),
                ),
            ],
            options={
                "verbose_name": "Настройки ИИ чатов",
                "verbose_name_plural": "Настройки ИИ чатов",
            },
        ),
        migrations.CreateModel(
            name="ChatAISuggestion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("prompt_context", models.JSONField(blank=True, default=dict, verbose_name="Контекст запроса")),
                ("category", models.CharField(blank=True, default="", max_length=64, verbose_name="Категория")),
                (
                    "confidence",
                    models.CharField(
                        choices=[("high", "Высокая"), ("medium", "Средняя"), ("low", "Низкая")],
                        default="low",
                        max_length=16,
                        verbose_name="Уверенность",
                    ),
                ),
                ("proposed_text", models.TextField(blank=True, default="", verbose_name="Предложенный текст")),
                ("final_text", models.TextField(blank=True, default="", verbose_name="Итоговый текст менеджера")),
                ("sources", models.JSONField(blank=True, default=list, verbose_name="Источники")),
                ("warnings", models.JSONField(blank=True, default=list, verbose_name="Предупреждения")),
                ("suggested_actions", models.JSONField(blank=True, default=list, verbose_name="Действия")),
                ("needs_escalation", models.BooleanField(default=False, verbose_name="Нужна эскалация")),
                ("auto_reply_allowed", models.BooleanField(default=False, verbose_name="Автоответ разрешён")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Черновик"),
                            ("inserted", "Вставлен"),
                            ("sent", "Отправлен"),
                            ("rejected", "Отклонён"),
                            ("edited", "Изменён"),
                            ("escalated", "Эскалация"),
                            ("error", "Ошибка"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=16,
                        verbose_name="Статус",
                    ),
                ),
                (
                    "manager_feedback",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("useful", "Полезно"),
                            ("partial", "Частично полезно"),
                            ("wrong", "Неверно"),
                            ("outdated", "Устаревшая информация"),
                            ("bad_source", "Не тот источник"),
                            ("no_auto", "Нельзя отвечать автоматически"),
                        ],
                        default="",
                        max_length=32,
                        verbose_name="Оценка менеджера",
                    ),
                ),
                ("feedback_note", models.TextField(blank=True, default="", verbose_name="Комментарий к оценке")),
                ("model_name", models.CharField(blank=True, default="stub", max_length=128, verbose_name="Модель/провайдер")),
                ("error_text", models.TextField(blank=True, default="", verbose_name="Ошибка")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True, verbose_name="Закрыт")),
                (
                    "agency",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="chat_ai_suggestions",
                        to="sku.agency",
                        verbose_name="Клиент",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="chat_ai_suggestions_requested",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто запросил",
                    ),
                ),
                (
                    "thread",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_suggestions",
                        to="client_cabinet.chatthread",
                        verbose_name="Тред",
                    ),
                ),
                (
                    "trigger_message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ai_suggestions_triggered",
                        to="client_cabinet.clientchatmessage",
                        verbose_name="Сообщение-триггер",
                    ),
                ),
            ],
            options={
                "verbose_name": "Черновик ИИ чата",
                "verbose_name_plural": "Черновики ИИ чатов",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="ChatAIKnowledgeCandidate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("question", models.TextField(blank=True, default="", verbose_name="Вопрос")),
                ("answer", models.TextField(blank=True, default="", verbose_name="Ответ")),
                ("category", models.CharField(blank=True, default="", max_length=64, verbose_name="Категория")),
                ("client_visible", models.BooleanField(default=False, verbose_name="Доступно клиенту")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "На проверке"),
                            ("approved", "Утверждено"),
                            ("rejected", "Отклонено"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                        verbose_name="Статус",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agency",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="chat_ai_kb_candidates",
                        to="sku.agency",
                        verbose_name="Клиент (если индивидуальный)",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="chat_ai_kb_candidates_created",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто добавил",
                    ),
                ),
                (
                    "moderated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="chat_ai_kb_candidates_moderated",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Модератор",
                    ),
                ),
                (
                    "suggestion",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="knowledge_candidates",
                        to="client_cabinet.chataisuggestion",
                        verbose_name="Черновик ИИ",
                    ),
                ),
            ],
            options={
                "verbose_name": "Кандидат в базу знаний ИИ",
                "verbose_name_plural": "Кандидаты в базу знаний ИИ",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="chataisuggestion",
            index=models.Index(fields=["agency", "created_at"], name="cc_ai_sugg_agency_created"),
        ),
        migrations.AddIndex(
            model_name="chataisuggestion",
            index=models.Index(fields=["thread", "status"], name="cc_ai_sugg_thread_status"),
        ),
    ]
