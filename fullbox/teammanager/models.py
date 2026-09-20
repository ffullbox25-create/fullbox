from django.conf import settings
from django.db import models


class TaskHandoff(models.Model):
    """История передачи / взятия задачи без дублирования Task."""

    ACTION_CLAIM = "claim"
    ACTION_TRANSFER = "transfer"
    ACTION_RETURN_QUEUE = "return_queue"
    ACTION_ASSIGN_DEPT = "assign_dept"
    ACTION_CHOICES = [
        (ACTION_CLAIM, "Взята в работу"),
        (ACTION_TRANSFER, "Передана"),
        (ACTION_RETURN_QUEUE, "Возвращена в очередь"),
        (ACTION_ASSIGN_DEPT, "Назначена подразделению"),
    ]

    task = models.ForeignKey(
        "todo.Task",
        on_delete=models.CASCADE,
        related_name="handoffs",
        verbose_name="Задача",
    )
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, db_index=True)
    from_employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="task_handoffs_from",
        verbose_name="Предыдущий ответственный",
    )
    to_employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="task_handoffs_to",
        verbose_name="Новый ответственный",
    )
    author = models.ForeignKey(
        "employees.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="task_handoffs_authored",
        verbose_name="Автор передачи",
    )
    reason = models.CharField("Причина", max_length=255, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    task_snapshot = models.JSONField("Состояние на момент передачи", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Передача задачи"
        verbose_name_plural = "Передачи задач"
        indexes = [
            models.Index(fields=["task", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} · task={self.task_id}"


class EmployeeCoverage(models.Model):
    """Временное замещение сотрудника внутри единого кабинета."""

    principal = models.ForeignKey(
        "employees.Employee",
        on_delete=models.CASCADE,
        related_name="coverages_as_principal",
        verbose_name="Основной сотрудник",
    )
    substitute = models.ForeignKey(
        "employees.Employee",
        on_delete=models.CASCADE,
        related_name="coverages_as_substitute",
        verbose_name="Замещающий",
    )
    valid_from = models.DateField("Начало замещения")
    valid_to = models.DateField("Окончание замещения", null=True, blank=True)
    task_types = models.CharField(
        "Типы задач (через запятую, пусто = все)",
        max_length=255,
        blank=True,
        help_text="receiving,processing,shipping,logistics",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_employee_coverages",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("-valid_from", "-id")
        verbose_name = "Замещение сотрудника"
        verbose_name_plural = "Замещения сотрудников"
        indexes = [
            models.Index(fields=["principal", "is_active", "valid_from"]),
            models.Index(fields=["substitute", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.principal_id} ← {self.substitute_id} ({self.valid_from})"

    def covers_type(self, task_type: str) -> bool:
        raw = (self.task_types or "").strip()
        if not raw:
            return True
        allowed = {part.strip() for part in raw.split(",") if part.strip()}
        return task_type in allowed


class TeamReportFavorite(models.Model):
    """Избранные отчёты ЛК менеджера по пользователю."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="team_report_favorites",
        verbose_name="Пользователь",
    )
    section = models.CharField("Раздел", max_length=48, db_index=True)
    report_code = models.CharField("Код отчёта", max_length=96, db_index=True)
    last_filters = models.JSONField("Последние фильтры", default=dict, blank=True)
    last_generated_at = models.DateTimeField("Последнее формирование", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("section", "report_code")
        verbose_name = "Избранный отчёт менеджера"
        verbose_name_plural = "Избранные отчёты менеджеров"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "section", "report_code"],
                name="uniq_team_report_favorite",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "section"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}: {self.section}/{self.report_code}"


class TeamSavedReport(models.Model):
    """Сохранённая конфигурация отчёта без хранения устаревших строк результата."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="team_saved_reports",
        verbose_name="Пользователь",
    )
    title = models.CharField("Название", max_length=160)
    section = models.CharField("Раздел", max_length=48, db_index=True)
    report_code = models.CharField("Код отчёта", max_length=96, db_index=True)
    filters = models.JSONField("Фильтры", default=dict, blank=True)
    sorting = models.JSONField("Сортировка", default=dict, blank=True)
    visible_columns = models.JSONField("Видимые столбцы", default=list, blank=True)
    export_format = models.CharField("Формат экспорта", max_length=16, default="xlsx")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-id")
        verbose_name = "Сохранённый отчёт менеджера"
        verbose_name_plural = "Сохранённые отчёты менеджеров"
        indexes = [
            models.Index(fields=["user", "-updated_at"]),
            models.Index(fields=["section", "report_code"]),
        ]

    def __str__(self) -> str:
        return self.title


class TeamReportExportJob(models.Model):
    """История формирования выгрузок отчётов ЛК менеджера."""

    STATUS_QUEUED = "queued"
    STATUS_BUILDING = "building"
    STATUS_READY = "ready"
    STATUS_ERROR = "error"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "В очереди"),
        (STATUS_BUILDING, "Формируется"),
        (STATUS_READY, "Готов"),
        (STATUS_ERROR, "Ошибка"),
        (STATUS_EXPIRED, "Срок хранения истёк"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="team_report_export_jobs",
        verbose_name="Пользователь",
    )
    section = models.CharField("Раздел", max_length=48, db_index=True)
    report_code = models.CharField("Код отчёта", max_length=96, db_index=True)
    report_title = models.CharField("Название отчёта", max_length=160)
    filters = models.JSONField("Фильтры", default=dict, blank=True)
    export_format = models.CharField("Формат", max_length=16, default="xlsx")
    status = models.CharField("Статус", max_length=24, choices=STATUS_CHOICES, default=STATUS_QUEUED, db_index=True)
    row_count = models.PositiveIntegerField("Строк", null=True, blank=True)
    file = models.FileField("Файл", upload_to="team_reports/", null=True, blank=True)
    error_text = models.TextField("Техническая ошибка", blank=True)
    started_at = models.DateTimeField("Старт", null=True, blank=True)
    finished_at = models.DateTimeField("Финиш", null=True, blank=True)
    expires_at = models.DateTimeField("Хранить до", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "Выгрузка отчёта менеджера"
        verbose_name_plural = "Выгрузки отчётов менеджеров"
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["section", "report_code"]),
        ]

    def __str__(self) -> str:
        return f"{self.report_title} · {self.get_status_display()}"
