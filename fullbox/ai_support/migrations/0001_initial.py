import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="Incident",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reporter_role", models.CharField(blank=True, max_length=64, verbose_name="Роль сотрудника")),
                ("zone", models.CharField(blank=True, max_length=80, verbose_name="Зона системы")),
                ("object_type", models.CharField(blank=True, max_length=64, verbose_name="Тип объекта")),
                ("object_id", models.CharField(blank=True, db_index=True, max_length=128, verbose_name="Номер заявки или объекта")),
                ("source_url", models.CharField(blank=True, max_length=1000, verbose_name="Страница ошибки")),
                ("page_title", models.CharField(blank=True, max_length=255, verbose_name="Название страницы")),
                ("description", models.TextField(verbose_name="Что произошло")),
                ("expected_result", models.TextField(blank=True, verbose_name="Что должно было произойти")),
                ("reproducible", models.CharField(choices=[("unknown", "Не знаю"), ("yes", "Да"), ("no", "Нет")], default="unknown", max_length=16, verbose_name="Можно повторить")),
                ("severity", models.CharField(choices=[("low", "Низкий"), ("normal", "Обычный"), ("high", "Высокий"), ("critical", "Критичный")], db_index=True, default="normal", max_length=16, verbose_name="Приоритет")),
                ("status", models.CharField(choices=[("new", "Новое"), ("needs_info", "Нужна информация"), ("diagnosing", "Идёт диагностика"), ("cause_found", "Причина найдена"), ("fix_prepared", "Исправление подготовлено"), ("waiting_approval", "Ожидает подтверждения"), ("fixed", "Исправлено"), ("verifying", "Проверяется сотрудником"), ("escalated", "Передано программисту"), ("closed", "Закрыто")], db_index=True, default="new", max_length=32, verbose_name="Статус")),
                ("context", models.JSONField(blank=True, default=dict, verbose_name="Технический контекст")),
                ("diagnosis", models.TextField(blank=True, verbose_name="Причина")),
                ("resolution", models.TextField(blank=True, verbose_name="Решение")),
                ("inventory_changed", models.BooleanField(default=False, verbose_name="Остатки изменялись")),
                ("movements_changed", models.BooleanField(default=False, verbose_name="Движения изменялись")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("reporter", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reported_ai_incidents", to=settings.AUTH_USER_MODEL, verbose_name="Сотрудник")),
            ],
            options={"verbose_name": "Обращение к ИИ-программисту", "verbose_name_plural": "Обращения к ИИ-программисту", "ordering": ("-updated_at", "-id")},
        ),
        migrations.CreateModel(
            name="IncidentMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sender_type", models.CharField(choices=[("employee", "Сотрудник"), ("agent", "ИИ-программист"), ("programmer", "Ответственный"), ("system", "Система")], max_length=16, verbose_name="Отправитель")),
                ("body", models.TextField(verbose_name="Сообщение")),
                ("metadata", models.JSONField(blank=True, default=dict, verbose_name="Метаданные")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("author", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="ai_incident_messages", to=settings.AUTH_USER_MODEL, verbose_name="Автор")),
                ("incident", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages", to="ai_support.incident", verbose_name="Обращение")),
            ],
            options={"verbose_name": "Сообщение обращения", "verbose_name_plural": "Сообщения обращений", "ordering": ("created_at", "id")},
        ),
        migrations.CreateModel(
            name="AgentAction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action_type", models.CharField(choices=[("read_logs", "Прочитать журналы"), ("read_database", "Проверить базу в режиме чтения"), ("inspect_services", "Проверить сервисы"), ("inspect_request", "Проверить заявку"), ("reproduce_in_sandbox", "Воспроизвести вне production"), ("prepare_patch", "Подготовить патч"), ("run_tests", "Запустить тесты"), ("deploy_patch", "Выложить патч"), ("restart_service", "Перезапустить сервис"), ("retry_idempotent_operation", "Повторить безопасную операцию"), ("correct_single_request_status", "Исправить статус одной заявки"), ("change_stock", "Изменить остатки"), ("change_movement", "Изменить движения"), ("bulk_data_change", "Массово изменить данные"), ("schema_change", "Изменить схему базы"), ("permission_change", "Изменить права"), ("new_feature", "Создать новую функцию"), ("architecture_change", "Изменить архитектуру")], max_length=64, verbose_name="Действие")),
                ("title", models.CharField(max_length=255, verbose_name="Описание")),
                ("payload", models.JSONField(blank=True, default=dict, verbose_name="Параметры")),
                ("policy_level", models.CharField(choices=[("automatic", "Разрешено агенту"), ("approval", "Требует подтверждения"), ("programmer", "Только программист")], editable=False, max_length=16, verbose_name="Уровень допуска")),
                ("status", models.CharField(choices=[("proposed", "Предложено"), ("waiting_approval", "Ожидает подтверждения"), ("approved", "Подтверждено"), ("running", "Выполняется"), ("succeeded", "Выполнено"), ("failed", "Ошибка"), ("rejected", "Отклонено"), ("escalated", "Передано программисту")], default="proposed", max_length=24, verbose_name="Статус")),
                ("result", models.JSONField(blank=True, default=dict, verbose_name="Результат")),
                ("error", models.TextField(blank=True, verbose_name="Ошибка")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="approved_ai_actions", to=settings.AUTH_USER_MODEL, verbose_name="Подтвердил")),
                ("incident", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="agent_actions", to="ai_support.incident", verbose_name="Обращение")),
            ],
            options={"verbose_name": "Действие ИИ-агента", "verbose_name_plural": "Действия ИИ-агента", "ordering": ("created_at", "id")},
        ),
        migrations.CreateModel(
            name="ActionApproval",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("decision", models.CharField(choices=[("pending", "Ожидает решения"), ("approved", "Подтверждено"), ("rejected", "Отклонено")], default="pending", max_length=16, verbose_name="Решение")),
                ("comment", models.TextField(blank=True, verbose_name="Комментарий")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("action", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="approval", to="ai_support.agentaction", verbose_name="Действие")),
                ("decided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="decided_ai_action_approvals", to=settings.AUTH_USER_MODEL, verbose_name="Принял решение")),
                ("requested_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="requested_ai_action_approvals", to=settings.AUTH_USER_MODEL, verbose_name="Запросил")),
            ],
            options={"verbose_name": "Подтверждение действия", "verbose_name_plural": "Подтверждения действий"},
        ),
        migrations.AddIndex(model_name="incident", index=models.Index(fields=["status", "-updated_at"], name="ai_inc_status_updated_idx")),
        migrations.AddIndex(model_name="incident", index=models.Index(fields=["reporter", "-updated_at"], name="ai_inc_reporter_updated_idx")),
    ]
