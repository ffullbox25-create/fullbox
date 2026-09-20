from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .policy import (
    POLICY_APPROVAL,
    POLICY_AUTOMATIC,
    POLICY_PROGRAMMER,
    policy_for_action,
)


class Incident(models.Model):
    STATUS_NEW = "new"
    STATUS_NEEDS_INFO = "needs_info"
    STATUS_DIAGNOSING = "diagnosing"
    STATUS_CAUSE_FOUND = "cause_found"
    STATUS_FIX_PREPARED = "fix_prepared"
    STATUS_WAITING_APPROVAL = "waiting_approval"
    STATUS_FIXED = "fixed"
    STATUS_VERIFYING = "verifying"
    STATUS_ESCALATED = "escalated"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_NEW, "Новое"),
        (STATUS_NEEDS_INFO, "Нужна информация"),
        (STATUS_DIAGNOSING, "Идёт диагностика"),
        (STATUS_CAUSE_FOUND, "Причина найдена"),
        (STATUS_FIX_PREPARED, "Исправление подготовлено"),
        (STATUS_WAITING_APPROVAL, "Ожидает подтверждения"),
        (STATUS_FIXED, "Исправлено"),
        (STATUS_VERIFYING, "Проверяется сотрудником"),
        (STATUS_ESCALATED, "Передано программисту"),
        (STATUS_CLOSED, "Закрыто"),
    ]

    SEVERITY_LOW = "low"
    SEVERITY_NORMAL = "normal"
    SEVERITY_HIGH = "high"
    SEVERITY_CRITICAL = "critical"
    SEVERITY_CHOICES = [
        (SEVERITY_LOW, "Низкий"),
        (SEVERITY_NORMAL, "Обычный"),
        (SEVERITY_HIGH, "Высокий"),
        (SEVERITY_CRITICAL, "Критичный"),
    ]

    REPRODUCIBLE_UNKNOWN = "unknown"
    REPRODUCIBLE_YES = "yes"
    REPRODUCIBLE_NO = "no"
    REPRODUCIBLE_CHOICES = [
        (REPRODUCIBLE_UNKNOWN, "Не знаю"),
        (REPRODUCIBLE_YES, "Да"),
        (REPRODUCIBLE_NO, "Нет"),
    ]

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reported_ai_incidents",
        verbose_name="Сотрудник",
    )
    reporter_role = models.CharField("Роль сотрудника", max_length=64, blank=True)
    zone = models.CharField("Зона системы", max_length=80, blank=True)
    object_type = models.CharField("Тип объекта", max_length=64, blank=True)
    object_id = models.CharField("Номер заявки или объекта", max_length=128, blank=True, db_index=True)
    source_url = models.CharField("Страница ошибки", max_length=1000, blank=True)
    page_title = models.CharField("Название страницы", max_length=255, blank=True)
    description = models.TextField("Что произошло")
    expected_result = models.TextField("Что должно было произойти", blank=True)
    reproducible = models.CharField(
        "Можно повторить",
        max_length=16,
        choices=REPRODUCIBLE_CHOICES,
        default=REPRODUCIBLE_UNKNOWN,
    )
    severity = models.CharField(
        "Приоритет",
        max_length=16,
        choices=SEVERITY_CHOICES,
        default=SEVERITY_NORMAL,
        db_index=True,
    )
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_NEW,
        db_index=True,
    )
    context = models.JSONField("Технический контекст", default=dict, blank=True)
    diagnosis = models.TextField("Причина", blank=True)
    resolution = models.TextField("Решение", blank=True)
    inventory_changed = models.BooleanField("Остатки изменялись", default=False)
    movements_changed = models.BooleanField("Движения изменялись", default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-updated_at", "-id")
        verbose_name = "Обращение к ИИ-программисту"
        verbose_name_plural = "Обращения к ИИ-программисту"
        indexes = [
            models.Index(fields=("status", "-updated_at"), name="ai_inc_status_updated_idx"),
            models.Index(fields=("reporter", "-updated_at"), name="ai_inc_reporter_updated_idx"),
        ]

    @property
    def number(self) -> str:
        return f"INC-{self.pk:06d}" if self.pk else "INC-NEW"

    @property
    def object_label(self) -> str:
        values = [value for value in (self.object_type, self.object_id) if value]
        return " · ".join(values) or "Объект не указан"

    def __str__(self) -> str:
        return f"{self.number}: {self.object_label}"


class IncidentMessage(models.Model):
    SENDER_EMPLOYEE = "employee"
    SENDER_AGENT = "agent"
    SENDER_PROGRAMMER = "programmer"
    SENDER_SYSTEM = "system"
    SENDER_CHOICES = [
        (SENDER_EMPLOYEE, "Сотрудник"),
        (SENDER_AGENT, "ИИ-программист"),
        (SENDER_PROGRAMMER, "Ответственный"),
        (SENDER_SYSTEM, "Система"),
    ]

    incident = models.ForeignKey(
        Incident,
        on_delete=models.CASCADE,
        related_name="messages",
        verbose_name="Обращение",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="ai_incident_messages",
        verbose_name="Автор",
    )
    sender_type = models.CharField("Отправитель", max_length=16, choices=SENDER_CHOICES)
    body = models.TextField("Сообщение")
    metadata = models.JSONField("Метаданные", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        verbose_name = "Сообщение обращения"
        verbose_name_plural = "Сообщения обращений"

    def __str__(self) -> str:
        return f"{self.incident.number} · {self.get_sender_type_display()}"


class AgentAction(models.Model):
    ACTION_CHOICES = [
        ("read_logs", "Прочитать журналы"),
        ("read_database", "Проверить базу в режиме чтения"),
        ("inspect_services", "Проверить сервисы"),
        ("inspect_request", "Проверить заявку"),
        ("reproduce_in_sandbox", "Воспроизвести вне production"),
        ("prepare_patch", "Подготовить патч"),
        ("run_tests", "Запустить тесты"),
        ("deploy_patch", "Выложить патч"),
        ("restart_service", "Перезапустить сервис"),
        ("retry_idempotent_operation", "Повторить безопасную операцию"),
        ("correct_single_request_status", "Исправить статус одной заявки"),
        ("change_stock", "Изменить остатки"),
        ("change_movement", "Изменить движения"),
        ("bulk_data_change", "Массово изменить данные"),
        ("schema_change", "Изменить схему базы"),
        ("permission_change", "Изменить права"),
        ("new_feature", "Создать новую функцию"),
        ("architecture_change", "Изменить архитектуру"),
    ]
    STATUS_PROPOSED = "proposed"
    STATUS_WAITING = "waiting_approval"
    STATUS_APPROVED = "approved"
    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_REJECTED = "rejected"
    STATUS_ESCALATED = "escalated"
    STATUS_CHOICES = [
        (STATUS_PROPOSED, "Предложено"),
        (STATUS_WAITING, "Ожидает подтверждения"),
        (STATUS_APPROVED, "Подтверждено"),
        (STATUS_RUNNING, "Выполняется"),
        (STATUS_SUCCEEDED, "Выполнено"),
        (STATUS_FAILED, "Ошибка"),
        (STATUS_REJECTED, "Отклонено"),
        (STATUS_ESCALATED, "Передано программисту"),
    ]
    POLICY_CHOICES = [
        (POLICY_AUTOMATIC, "Разрешено агенту"),
        (POLICY_APPROVAL, "Требует подтверждения"),
        (POLICY_PROGRAMMER, "Только программист"),
    ]

    incident = models.ForeignKey(
        Incident,
        on_delete=models.CASCADE,
        related_name="agent_actions",
        verbose_name="Обращение",
    )
    action_type = models.CharField("Действие", max_length=64, choices=ACTION_CHOICES)
    title = models.CharField("Описание", max_length=255)
    payload = models.JSONField("Параметры", default=dict, blank=True)
    policy_level = models.CharField(
        "Уровень допуска",
        max_length=16,
        choices=POLICY_CHOICES,
        editable=False,
    )
    status = models.CharField(
        "Статус",
        max_length=24,
        choices=STATUS_CHOICES,
        default=STATUS_PROPOSED,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="approved_ai_actions",
        verbose_name="Подтвердил",
    )
    result = models.JSONField("Результат", default=dict, blank=True)
    error = models.TextField("Ошибка", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at", "id")
        verbose_name = "Действие ИИ-агента"
        verbose_name_plural = "Действия ИИ-агента"

    @property
    def policy(self):
        return policy_for_action(self.action_type)

    @property
    def executable(self) -> bool:
        if self.policy_level == POLICY_PROGRAMMER:
            return False
        if self.policy_level == POLICY_APPROVAL:
            return bool(self.approved_by_id)
        return True

    def clean(self):
        super().clean()
        policy = policy_for_action(self.action_type)
        self.policy_level = policy.level
        execution_states = {self.STATUS_APPROVED, self.STATUS_RUNNING, self.STATUS_SUCCEEDED}
        if self.status in execution_states and policy.programmer_required:
            raise ValidationError(
                {"status": "Защищённое действие нельзя выполнять через ИИ-агента."}
            )
        if self.status in execution_states and policy.human_approval_required and not self.approved_by_id:
            raise ValidationError({"approved_by": "Сначала требуется подтверждение человека."})

    def save(self, *args, **kwargs):
        self.policy_level = policy_for_action(self.action_type).level
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.incident.number} · {self.get_action_type_display()}"


class ActionApproval(models.Model):
    DECISION_PENDING = "pending"
    DECISION_APPROVED = "approved"
    DECISION_REJECTED = "rejected"
    DECISION_CHOICES = [
        (DECISION_PENDING, "Ожидает решения"),
        (DECISION_APPROVED, "Подтверждено"),
        (DECISION_REJECTED, "Отклонено"),
    ]

    action = models.OneToOneField(
        AgentAction,
        on_delete=models.CASCADE,
        related_name="approval",
        verbose_name="Действие",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="requested_ai_action_approvals",
        verbose_name="Запросил",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="decided_ai_action_approvals",
        verbose_name="Принял решение",
    )
    decision = models.CharField(
        "Решение",
        max_length=16,
        choices=DECISION_CHOICES,
        default=DECISION_PENDING,
    )
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        verbose_name = "Подтверждение действия"
        verbose_name_plural = "Подтверждения действий"

    def __str__(self) -> str:
        return f"{self.action} · {self.get_decision_display()}"
