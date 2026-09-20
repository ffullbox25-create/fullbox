import os
import re
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from sku.models import Agency


_ATTACHMENT_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def other_attachment_upload_to(instance, filename: str) -> str:
    original = os.path.basename(str(filename or "file"))
    stem, ext = os.path.splitext(original)
    safe_stem = _ATTACHMENT_FILENAME_RE.sub("_", stem).strip("._") or "file"
    safe_ext = _ATTACHMENT_FILENAME_RE.sub("", ext).lower()[:16]
    order_number = _ATTACHMENT_FILENAME_RE.sub("_", str(getattr(instance, "order_id", "") or "other"))
    period = timezone.now().strftime("%Y/%m")
    return f"other_attachments/{order_number}/{period}/{safe_stem}{safe_ext}"


def finance_document_upload_to(instance, filename: str) -> str:
    original = os.path.basename(str(filename or "file"))
    stem, ext = os.path.splitext(original)
    safe_stem = _ATTACHMENT_FILENAME_RE.sub("_", stem).strip("._") or "file"
    safe_ext = _ATTACHMENT_FILENAME_RE.sub("", ext).lower()[:16]
    agency_id = getattr(instance, "agency_id", None) or "client"
    kind = _ATTACHMENT_FILENAME_RE.sub("_", str(getattr(instance, "doc_kind", "") or "doc"))
    period = timezone.now().strftime("%Y/%m")
    return f"finance_documents/{agency_id}/{kind}/{period}/{safe_stem}{safe_ext}"


class OtherRequestAttachment(models.Model):
    PURPOSE_CLIENT = "client"
    PURPOSE_INTERNAL = "internal"
    PURPOSE_RESULT = "result"
    PURPOSE_CHOICES = [
        (PURPOSE_CLIENT, "Файл клиента"),
        (PURPOSE_INTERNAL, "Внутренний файл"),
        (PURPOSE_RESULT, "Результат выполнения"),
    ]

    order_id = models.CharField("Номер заявки", max_length=64, db_index=True)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="other_request_attachments",
        verbose_name="Клиент",
    )
    request = models.ForeignKey(
        "OtherRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attachments",
        verbose_name="Заявка",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="other_request_attachments",
        verbose_name="Кто загрузил",
    )
    purpose = models.CharField(
        "Назначение",
        max_length=16,
        choices=PURPOSE_CHOICES,
        default=PURPOSE_CLIENT,
        db_index=True,
    )
    file = models.FileField("Файл", upload_to=other_attachment_upload_to)
    file_size = models.PositiveIntegerField("Размер, байт", default=0)
    uploaded_at = models.DateTimeField("Загружен", auto_now_add=True)

    class Meta:
        verbose_name = "Файл другой заявки"
        verbose_name_plural = "Файлы других заявок"
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["order_id", "uploaded_at"]),
            models.Index(fields=["agency", "order_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.order_id}: {self.filename}"

    @property
    def filename(self) -> str:
        return os.path.basename(self.file.name)


class OtherRequestCategory(models.Model):
    code = models.SlugField("Код", max_length=64, unique=True)
    title = models.CharField("Название", max_length=128)
    is_active = models.BooleanField("Активна", default=True)
    default_department = models.CharField("Подразделение по умолчанию", max_length=32, blank=True, default="warehouse")
    default_sla_hours = models.PositiveIntegerField("Типовой срок SLA, ч", default=24)
    require_result_photo = models.BooleanField("Нужно фото результата", default=False)
    require_result_file = models.BooleanField("Нужен файл результата", default=False)
    require_qty = models.BooleanField("Нужно количество", default=False)
    can_be_paid = models.BooleanField("Может быть платной", default=True)
    default_billing_service_code = models.CharField("Услуга биллинга по умолчанию", max_length=64, blank=True)
    sort_order = models.PositiveIntegerField("Порядок", default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Категория другой заявки"
        verbose_name_plural = "Категории других заявок"
        ordering = ["sort_order", "title"]

    def __str__(self) -> str:
        return self.title


class OtherRequest(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_AWAITING_MANAGER = "awaiting_manager"
    STATUS_NEED_CLIENT_INFO = "need_client_info"
    STATUS_APPROVED = "approved"
    STATUS_AWAITING_DEPARTMENT = "awaiting_department"
    STATUS_WAREHOUSE_ACCEPTED = "warehouse_accepted"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_PAUSED = "paused"
    STATUS_DONE_BY_DEPARTMENT = "done_by_department"
    STATUS_AWAITING_MANAGER_CHECK = "awaiting_manager_check"
    STATUS_REWORK = "rework"
    STATUS_CLOSED = "closed"
    STATUS_CANCELLED = "cancelled"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_AWAITING_MANAGER, "Ожидает менеджера"),
        (STATUS_NEED_CLIENT_INFO, "Требуется уточнение"),
        (STATUS_APPROVED, "Подтверждена менеджером"),
        (STATUS_AWAITING_DEPARTMENT, "Ожидает принятия подразделением"),
        (STATUS_WAREHOUSE_ACCEPTED, "Принято складом"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_PAUSED, "Приостановлена"),
        (STATUS_DONE_BY_DEPARTMENT, "Выполнена подразделением"),
        (STATUS_AWAITING_MANAGER_CHECK, "Ожидает проверки менеджером"),
        (STATUS_REWORK, "Возвращена на доработку"),
        (STATUS_CLOSED, "Закрыта"),
        (STATUS_CANCELLED, "Отменена"),
        (STATUS_REJECTED, "Отклонена"),
    ]

    PRIORITY_LOW = "low"
    PRIORITY_NORMAL = "normal"
    PRIORITY_HIGH = "high"
    PRIORITY_URGENT = "urgent"
    PRIORITY_CHOICES = [
        (PRIORITY_LOW, "Низкий"),
        (PRIORITY_NORMAL, "Обычный"),
        (PRIORITY_HIGH, "Высокий"),
        (PRIORITY_URGENT, "Срочный"),
    ]

    DEPARTMENT_WAREHOUSE = "warehouse"
    DEPARTMENT_PROCESSING = "processing"
    DEPARTMENT_PACKING = "packing"
    DEPARTMENT_LOGISTICS = "logistics"
    DEPARTMENT_MANAGERS = "managers"
    DEPARTMENT_ACCOUNTING = "accounting"
    DEPARTMENT_IT = "it"
    DEPARTMENT_OTHER = "other"
    DEPARTMENT_CHOICES = [
        (DEPARTMENT_WAREHOUSE, "Склад"),
        (DEPARTMENT_PROCESSING, "Обработка"),
        (DEPARTMENT_PACKING, "Упаковка / короба"),
        (DEPARTMENT_LOGISTICS, "Логистика"),
        (DEPARTMENT_MANAGERS, "Менеджеры"),
        (DEPARTMENT_ACCOUNTING, "Бухгалтерия"),
        (DEPARTMENT_IT, "IT"),
        (DEPARTMENT_OTHER, "Другое"),
    ]

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    public_number = models.CharField("Номер", max_length=64, unique=True, db_index=True)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="other_requests",
        verbose_name="Клиент",
    )
    category = models.ForeignKey(
        OtherRequestCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requests",
        verbose_name="Категория",
    )
    category_code = models.CharField("Код категории", max_length=64, blank=True, db_index=True)
    title = models.CharField("Название задачи", max_length=255, blank=True)
    description = models.TextField("Описание", blank=True)
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
        db_index=True,
    )
    department = models.CharField(
        "Подразделение",
        max_length=32,
        choices=DEPARTMENT_CHOICES,
        default=DEPARTMENT_WAREHOUSE,
        db_index=True,
    )
    priority = models.CharField(
        "Приоритет",
        max_length=16,
        choices=PRIORITY_CHOICES,
        default=PRIORITY_NORMAL,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="other_requests_created",
        verbose_name="Автор",
    )
    manager = models.ForeignKey(
        "employees.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="other_requests_managed",
        verbose_name="Менеджер клиента",
    )
    assignee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="other_requests_assigned",
        verbose_name="Ответственный",
    )
    due_at = models.DateTimeField("Плановый срок", null=True, blank=True, db_index=True)
    started_at = models.DateTimeField("Начало работы", null=True, blank=True)
    completed_at = models.DateTimeField("Фактическое выполнение", null=True, blank=True)
    closed_at = models.DateTimeField("Закрыта", null=True, blank=True)
    cancelled_at = models.DateTimeField("Отменена", null=True, blank=True)
    pause_reason = models.TextField("Причина приостановки", blank=True)
    cancel_reason = models.TextField("Причина отмены", blank=True)
    reject_reason = models.TextField("Причина отклонения", blank=True)
    rework_reason = models.TextField("Причина возврата", blank=True)
    result_outcome = models.CharField("Итог выполнения", max_length=64, blank=True)
    result_comment = models.TextField("Комментарий исполнителя", blank=True)
    result_qty = models.DecimalField("Факт. количество", max_digits=12, decimal_places=3, null=True, blank=True)
    result_unit = models.CharField("Ед. изм.", max_length=32, blank=True)
    is_paid = models.BooleanField("Платная задача", default=False)
    billing_service_code = models.CharField("Код услуги", max_length=64, blank=True)
    billing_qty = models.DecimalField("Кол-во услуги", max_digits=12, decimal_places=3, null=True, blank=True)
    billing_amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2, null=True, blank=True)
    billing_status = models.CharField("Статус начисления", max_length=32, blank=True)
    internal_note = models.TextField("Внутренний комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        verbose_name = "Другая заявка"
        verbose_name_plural = "Другие заявки"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "due_at"]),
            models.Index(fields=["agency", "status"]),
            models.Index(fields=["department", "status"]),
            models.Index(fields=["assignee", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.public_number}: {self.display_title}"

    @property
    def display_title(self) -> str:
        title = str(self.title or "").strip()
        if title:
            return title
        desc = str(self.description or "").strip()
        if desc:
            words = desc.split()
            return " ".join(words[:8]) + ("…" if len(words) > 8 else "")
        if self.category_id:
            return str(self.category.title)
        return "Другая заявка"

    @property
    def is_overdue(self) -> bool:
        if self.status in {
            self.STATUS_CLOSED,
            self.STATUS_CANCELLED,
            self.STATUS_REJECTED,
            self.STATUS_DRAFT,
        }:
            return False
        if not self.due_at:
            return False
        return timezone.now() > self.due_at

    @property
    def status_label(self) -> str:
        return dict(self.STATUS_CHOICES).get(self.status, self.status)

    @property
    def priority_label(self) -> str:
        return dict(self.PRIORITY_CHOICES).get(self.priority, self.priority)

    @property
    def department_label(self) -> str:
        return dict(self.DEPARTMENT_CHOICES).get(self.department, self.department)

    @property
    def detail_url(self) -> str:
        return f"/team-manager/other-requests/{self.public_number}/"

    @property
    def wms_url(self) -> str:
        return f"/orders/other/{self.public_number}/"


class OtherRequestLink(models.Model):
    LINK_RECEIVING = "receiving"
    LINK_PROCESSING = "processing"
    LINK_SHIPPING = "shipping"
    LINK_TRIP = "trip"
    LINK_SKU = "sku"
    LINK_BARCODE = "barcode"
    LINK_BOX = "box"
    LINK_PALLET = "pallet"
    LINK_CELL = "cell"
    LINK_SUPPLY = "supply"
    LINK_OTHER = "other"
    LINK_CHOICES = [
        (LINK_RECEIVING, "Приёмка"),
        (LINK_PROCESSING, "Обработка"),
        (LINK_SHIPPING, "Отгрузка"),
        (LINK_TRIP, "Рейс"),
        (LINK_SKU, "Артикул"),
        (LINK_BARCODE, "ШК товара"),
        (LINK_BOX, "Короб"),
        (LINK_PALLET, "Палета"),
        (LINK_CELL, "Ячейка"),
        (LINK_SUPPLY, "Поставка"),
        (LINK_OTHER, "Другое"),
    ]

    request = models.ForeignKey(OtherRequest, on_delete=models.CASCADE, related_name="links")
    link_type = models.CharField("Тип связи", max_length=32, choices=LINK_CHOICES)
    object_key = models.CharField("Ключ объекта", max_length=128)
    label = models.CharField("Подпись", max_length=255, blank=True)
    url = models.CharField("Ссылка", max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Связанный объект другой заявки"
        verbose_name_plural = "Связанные объекты других заявок"
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.request.public_number}: {self.link_type}={self.object_key}"


class OtherRequestComment(models.Model):
    VISIBILITY_CLIENT = "client"
    VISIBILITY_INTERNAL = "internal"
    VISIBILITY_CHOICES = [
        (VISIBILITY_CLIENT, "Клиенту"),
        (VISIBILITY_INTERNAL, "Внутренний"),
    ]

    request = models.ForeignKey(OtherRequest, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="other_request_comments",
    )
    visibility = models.CharField(
        "Видимость",
        max_length=16,
        choices=VISIBILITY_CHOICES,
        default=VISIBILITY_INTERNAL,
        db_index=True,
    )
    text = models.TextField("Текст")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Комментарий другой заявки"
        verbose_name_plural = "Комментарии других заявок"
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.request.public_number}: {self.text[:40]}"


class ClientFinanceDocument(models.Model):
    KIND_INVOICE = "invoice"
    KIND_UPD = "upd"
    KIND_ACT = "act"
    KIND_CHOICES = [
        (KIND_INVOICE, "Счёт"),
        (KIND_UPD, "УПД"),
        (KIND_ACT, "Акт"),
    ]

    STATUS_DRAFT = "draft"
    STATUS_ISSUED = "issued"
    STATUS_PAID = "paid"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_ISSUED, "Выставлен"),
        (STATUS_PAID, "Оплачен"),
        (STATUS_CANCELLED, "Отменён"),
    ]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="finance_documents",
        verbose_name="Клиент",
    )
    doc_kind = models.CharField("Тип", max_length=16, choices=KIND_CHOICES, default=KIND_INVOICE)
    title = models.CharField("Название", max_length=255)
    number = models.CharField("Номер", max_length=64, blank=True)
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2, default=Decimal("0"))
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default=STATUS_ISSUED)
    period = models.CharField("Период", max_length=7, blank=True, help_text="YYYY-MM")
    issued_at = models.DateField("Дата", null=True, blank=True)
    order_type = models.CharField("Тип заявки", max_length=32, blank=True)
    order_id = models.CharField("Номер заявки", max_length=64, blank=True)
    external_url = models.CharField("Внешняя ссылка", max_length=512, blank=True)
    file = models.FileField("Файл", upload_to=finance_document_upload_to, blank=True, null=True)
    note = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Финансовый документ"
        verbose_name_plural = "Финансовые документы"
        ordering = ["-issued_at", "-id"]
        indexes = [
            models.Index(fields=["agency", "doc_kind", "issued_at"]),
            models.Index(fields=["agency", "period"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_doc_kind_display()} {self.number or self.title}"


class ClientServiceCharge(models.Model):
    TYPE_STORAGE = "storage"
    TYPE_RECEIVING = "receiving"
    TYPE_SHIPPING = "shipping"
    TYPE_EXTRA = "extra"
    TYPE_CHOICES = [
        (TYPE_STORAGE, "Хранение"),
        (TYPE_RECEIVING, "Приёмка"),
        (TYPE_SHIPPING, "Отгрузка"),
        (TYPE_EXTRA, "Доп. услуги"),
    ]

    STATUS_OPEN = "open"
    STATUS_INVOICED = "invoiced"
    STATUS_PAID = "paid"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Открыто"),
        (STATUS_INVOICED, "В счёте"),
        (STATUS_PAID, "Оплачено"),
        (STATUS_CANCELLED, "Отменено"),
    ]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="service_charges",
        verbose_name="Клиент",
    )
    service_type = models.CharField("Тип услуги", max_length=16, choices=TYPE_CHOICES, default=TYPE_EXTRA)
    description = models.CharField("Описание", max_length=512)
    amount = models.DecimalField("Сумма", max_digits=12, decimal_places=2, default=Decimal("0"))
    period = models.CharField("Период", max_length=7, blank=True, help_text="YYYY-MM")
    charged_at = models.DateField("Дата начисления", null=True, blank=True)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    order_type = models.CharField("Тип заявки", max_length=32, blank=True)
    order_id = models.CharField("Номер заявки", max_length=64, blank=True)
    source_key = models.CharField(
        "Ключ источника",
        max_length=128,
        blank=True,
        db_index=True,
        help_text="Идемпотентный ключ автосоздания (например shipping-tn:12)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Начисление услуги"
        verbose_name_plural = "Начисления услуг"
        ordering = ["-charged_at", "-id"]
        indexes = [
            models.Index(fields=["agency", "period", "service_type"]),
            models.Index(fields=["agency", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "source_key"],
                condition=~models.Q(source_key=""),
                name="client_cabinet_charge_unique_source",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_service_type_display()}: {self.amount}"


def chat_attachment_upload_to(instance, filename: str) -> str:
    original = os.path.basename(str(filename or "file"))
    stem, ext = os.path.splitext(original)
    safe_stem = _ATTACHMENT_FILENAME_RE.sub("_", stem).strip("._") or "file"
    safe_ext = _ATTACHMENT_FILENAME_RE.sub("", ext).lower()[:16]
    agency_id = getattr(getattr(instance, "message", None), "agency_id", None) or "client"
    period = timezone.now().strftime("%Y/%m")
    return f"chat_attachments/{agency_id}/{period}/{safe_stem}{safe_ext}"


class ClientNotification(models.Model):
    TYPE_STATUS = "status"
    TYPE_COMMENT = "comment"
    TYPE_FINANCE = "finance"
    TYPE_CHAT = "chat"
    TYPE_SYSTEM = "system"
    TYPE_CHOICES = [
        (TYPE_STATUS, "Статус"),
        (TYPE_COMMENT, "Комментарий"),
        (TYPE_FINANCE, "Финансы"),
        (TYPE_CHAT, "Чат"),
        (TYPE_SYSTEM, "Система"),
    ]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="lk_notifications",
        verbose_name="Клиент",
    )
    notif_type = models.CharField("Тип", max_length=16, choices=TYPE_CHOICES, default=TYPE_SYSTEM)
    title = models.CharField("Заголовок", max_length=255)
    text = models.TextField("Текст", blank=True)
    detail_url = models.CharField("Ссылка", max_length=512, blank=True)
    priority = models.CharField("Приоритет", max_length=16, default="normal")
    source_key = models.CharField("Ключ источника", max_length=128, blank=True, db_index=True)
    is_read = models.BooleanField("Прочитано", default=False)
    read_at = models.DateTimeField("Прочитано в", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Уведомление ЛК"
        verbose_name_plural = "Уведомления ЛК"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["agency", "is_read", "created_at"]),
            models.Index(fields=["agency", "notif_type"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "source_key"],
                condition=~models.Q(source_key=""),
                name="client_cabinet_notification_unique_source",
            )
        ]

    def __str__(self) -> str:
        return self.title


class ChatThread(models.Model):
    """Единый тред коммуникаций: общий чат клиента, чат заявки, внутренний, задача."""

    KIND_CLIENT_GENERAL = "client_general"
    KIND_ORDER_CLIENT = "order_client"
    KIND_ORDER_INTERNAL = "order_internal"
    KIND_TASK = "task"
    KIND_TRIP = "trip"
    KIND_MANUAL = "manual"
    KIND_CHOICES = [
        (KIND_CLIENT_GENERAL, "Клиент — FullBox"),
        (KIND_ORDER_CLIENT, "Чат заявки (клиент)"),
        (KIND_ORDER_INTERNAL, "Внутренний чат заявки"),
        (KIND_TASK, "Чат задачи"),
        (KIND_TRIP, "Чат рейса"),
        (KIND_MANUAL, "Ручной чат"),
    ]

    STATUS_NEW = "new"
    STATUS_NEEDS_STAFF = "needs_staff"
    STATUS_WAIT_CLIENT = "wait_client"
    STATUS_WAIT_WAREHOUSE = "wait_warehouse"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RESOLVED = "resolved"
    STATUS_CLOSED = "closed"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [
        (STATUS_NEW, "Новый"),
        (STATUS_NEEDS_STAFF, "Требует ответа FullBox"),
        (STATUS_WAIT_CLIENT, "Ожидается ответ клиента"),
        (STATUS_WAIT_WAREHOUSE, "Ожидается ответ склада"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_RESOLVED, "Решён"),
        (STATUS_CLOSED, "Закрыт"),
        (STATUS_ARCHIVED, "Архив"),
    ]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="chat_threads",
        verbose_name="Клиент",
    )
    kind = models.CharField("Тип", max_length=32, choices=KIND_CHOICES, db_index=True)
    title = models.CharField("Название", max_length=255, blank=True)
    order_type = models.CharField("Тип заявки", max_length=32, blank=True, db_index=True)
    order_id = models.CharField("Номер заявки", max_length=64, blank=True, db_index=True)
    task = models.ForeignKey(
        "todo.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chat_threads",
        verbose_name="Задача",
    )
    conversation_status = models.CharField(
        "Статус обращения",
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_NEW,
        db_index=True,
    )
    is_archived = models.BooleanField("Архив", default=False, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_chat_threads",
        verbose_name="Создал",
    )
    last_message_at = models.DateTimeField("Последнее сообщение", null=True, blank=True, db_index=True)
    pinned_message = models.ForeignKey(
        "ClientChatMessage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pinned_in_threads",
        verbose_name="Закреплённое сообщение",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Чат-тред"
        verbose_name_plural = "Чат-треды"
        ordering = ["-last_message_at", "-updated_at", "-id"]
        indexes = [
            models.Index(fields=["agency", "kind", "is_archived"]),
            models.Index(fields=["order_type", "order_id"]),
            models.Index(fields=["agency", "last_message_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["agency"],
                condition=models.Q(kind="client_general"),
                name="uniq_chat_thread_client_general",
            ),
            models.UniqueConstraint(
                fields=["agency", "kind", "order_type", "order_id"],
                condition=models.Q(kind__in=["order_client", "order_internal"])
                & ~models.Q(order_id=""),
                name="uniq_chat_thread_order_kind",
            ),
            models.UniqueConstraint(
                fields=["kind", "order_type", "order_id"],
                condition=models.Q(kind="trip") & ~models.Q(order_id=""),
                name="uniq_chat_thread_trip",
            ),
        ]

    def __str__(self) -> str:
        return self.title or f"{self.get_kind_display()} · {self.agency_id}"

    @property
    def is_client_visible_thread(self) -> bool:
        return self.kind in {self.KIND_CLIENT_GENERAL, self.KIND_ORDER_CLIENT}


class ClientChatMessage(models.Model):
    ROLE_CLIENT = "client"
    ROLE_STAFF = "staff"
    ROLE_SYSTEM = "system"
    ROLE_CHOICES = [
        (ROLE_CLIENT, "Клиент"),
        (ROLE_STAFF, "FullBox"),
        (ROLE_SYSTEM, "Система"),
    ]

    VISIBILITY_CLIENT = "client"
    VISIBILITY_INTERNAL = "internal"
    VISIBILITY_SYSTEM = "system"
    VISIBILITY_CHOICES = [
        (VISIBILITY_CLIENT, "Видно клиенту"),
        (VISIBILITY_INTERNAL, "Только FullBox"),
        (VISIBILITY_SYSTEM, "Системное"),
    ]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="chat_messages",
        verbose_name="Клиент",
    )
    thread = models.ForeignKey(
        ChatThread,
        on_delete=models.CASCADE,
        related_name="messages",
        null=True,
        blank=True,
        verbose_name="Тред",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="client_chat_messages",
        verbose_name="Автор",
    )
    author_role = models.CharField("Роль автора", max_length=16, choices=ROLE_CHOICES, default=ROLE_CLIENT)
    visibility = models.CharField(
        "Видимость",
        max_length=16,
        choices=VISIBILITY_CHOICES,
        default=VISIBILITY_CLIENT,
        db_index=True,
    )
    public_id = models.UUIDField("UUID", default=uuid.uuid4, editable=False, db_index=True)
    text = models.TextField("Текст", blank=True)
    reply_to = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replies",
        verbose_name="Ответ на",
    )
    idempotency_key = models.CharField("Idempotency key", max_length=64, blank=True, db_index=True)
    delivered_at = models.DateTimeField("Доставлено", null=True, blank=True)
    is_read_by_client = models.BooleanField("Прочитано клиентом", default=False)
    is_read_by_staff = models.BooleanField("Прочитано сотрудником", default=False)
    is_deleted = models.BooleanField("Помечено удалённым", default=False)
    deleted_at = models.DateTimeField("Удалено", null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deleted_chat_messages",
        verbose_name="Удалил",
    )
    edited_at = models.DateTimeField("Отредактировано", null=True, blank=True)
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="edited_chat_messages",
        verbose_name="Редактировал",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Сообщение чата ЛК"
        verbose_name_plural = "Сообщения чата ЛК"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["agency", "created_at"]),
            models.Index(fields=["agency", "author_role", "is_read_by_client"]),
            models.Index(fields=["thread", "created_at"]),
            models.Index(fields=["thread", "is_read_by_staff", "author_role"]),
            models.Index(fields=["thread", "idempotency_key"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["thread", "author", "idempotency_key"],
                condition=~models.Q(idempotency_key="") & models.Q(thread__isnull=False),
                name="uniq_chat_msg_idempotency",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.agency_id}: {self.text[:40]}"

    @property
    def client_can_see(self) -> bool:
        if self.is_deleted:
            return False
        return self.visibility in {self.VISIBILITY_CLIENT, self.VISIBILITY_SYSTEM}


class ClientChatAttachment(models.Model):
    message = models.ForeignKey(
        ClientChatMessage,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name="Сообщение",
    )
    file = models.FileField("Файл", upload_to=chat_attachment_upload_to)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Вложение чата ЛК"
        verbose_name_plural = "Вложения чата ЛК"
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return self.filename

    @property
    def filename(self) -> str:
        return os.path.basename(self.file.name)


class ChatMessageReaction(models.Model):
    message = models.ForeignKey(
        ClientChatMessage,
        on_delete=models.CASCADE,
        related_name="reactions",
        verbose_name="Сообщение",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_reactions",
        verbose_name="Пользователь",
    )
    emoji = models.CharField("Реакция", max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Реакция на сообщение"
        verbose_name_plural = "Реакции на сообщения"
        constraints = [
            models.UniqueConstraint(fields=["message", "user", "emoji"], name="uniq_chat_reaction"),
        ]
        indexes = [models.Index(fields=["message", "emoji"])]


class ChatMessageReadReceipt(models.Model):
    message = models.ForeignKey(
        ClientChatMessage,
        on_delete=models.CASCADE,
        related_name="read_receipts",
        verbose_name="Сообщение",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_read_receipts",
        verbose_name="Пользователь",
    )
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Прочтение сообщения"
        verbose_name_plural = "Прочтения сообщений"
        constraints = [
            models.UniqueConstraint(fields=["message", "user"], name="uniq_chat_read_receipt"),
        ]
        indexes = [models.Index(fields=["user", "read_at"]), models.Index(fields=["message", "read_at"])]


class ChatTelegramBotConfig(models.Model):
    """Singleton: настройки Telegram-бота для чатов (заполняет директор)."""

    bot_token = models.CharField("Токен бота", max_length=256, blank=True, default="")
    bot_username = models.CharField("Username бота", max_length=128, blank=True, default="")
    webhook_secret = models.CharField("Secret webhook", max_length=256, blank=True, default="")
    alert_chat_id = models.CharField("Общий alert chat id", max_length=64, blank=True, default="")
    lk_base_url = models.CharField("Базовый URL ЛК", max_length=255, blank=True, default="")
    is_enabled = models.BooleanField("Включено", default=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_chat_telegram_configs",
        verbose_name="Кто обновил",
    )

    class Meta:
        verbose_name = "Настройки Telegram-бота чатов"
        verbose_name_plural = "Настройки Telegram-бота чатов"

    def __str__(self) -> str:
        return "Telegram бот чатов"

    @classmethod
    def load(cls) -> "ChatTelegramBotConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ChatNotificationPreference(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_notification_pref",
        verbose_name="Пользователь",
    )
    sound_enabled = models.BooleanField("Звук", default=True)
    sound_muted_until = models.DateTimeField("Звук выключен до", null=True, blank=True)
    browser_notifications = models.BooleanField("Браузерные уведомления", default=False)
    notify_client_messages = models.BooleanField("Сообщения клиентов", default=True)
    notify_internal = models.BooleanField("Внутренние сообщения", default=True)
    notify_mentions = models.BooleanField("Упоминания", default=True)
    telegram_chat_id = models.CharField("Telegram chat id", max_length=64, blank=True, default="")
    telegram_enabled = models.BooleanField("Telegram включён", default=False)
    telegram_link_code = models.CharField("Код привязки Telegram", max_length=32, blank=True, default="")
    telegram_link_expires_at = models.DateTimeField("Код привязки действует до", null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Настройки уведомлений чата"
        verbose_name_plural = "Настройки уведомлений чата"

    def sound_is_active(self) -> bool:
        if not self.sound_enabled:
            return False
        if self.sound_muted_until and self.sound_muted_until > timezone.now():
            return False
        return True


class ChatMessageMention(models.Model):
    message = models.ForeignKey(
        ClientChatMessage,
        on_delete=models.CASCADE,
        related_name="mentions",
        verbose_name="Сообщение",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_mentions",
        verbose_name="Упомянутый",
    )
    mention_text = models.CharField("Текст упоминания", max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Упоминание в чате"
        verbose_name_plural = "Упоминания в чате"
        constraints = [
            models.UniqueConstraint(fields=["message", "user"], name="uniq_chat_mention"),
        ]
        indexes = [models.Index(fields=["user", "created_at"])]


class ChatMessageAudit(models.Model):
    ACTION_EDIT = "edit"
    ACTION_DELETE = "delete"
    ACTION_PIN = "pin"
    ACTION_UNPIN = "unpin"
    ACTION_CHOICES = [
        (ACTION_EDIT, "Редактирование"),
        (ACTION_DELETE, "Удаление"),
        (ACTION_PIN, "Закрепление"),
        (ACTION_UNPIN, "Открепление"),
    ]

    thread = models.ForeignKey(
        ChatThread,
        on_delete=models.CASCADE,
        related_name="message_audits",
        verbose_name="Тред",
        null=True,
        blank=True,
    )
    message = models.ForeignKey(
        ClientChatMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audits",
        verbose_name="Сообщение",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chat_message_audits",
        verbose_name="Кто",
    )
    action = models.CharField("Действие", max_length=16, choices=ACTION_CHOICES, db_index=True)
    old_text = models.TextField("Было", blank=True)
    new_text = models.TextField("Стало", blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Аудит сообщения чата"
        verbose_name_plural = "Аудит сообщений чата"
        ordering = ["-created_at", "-id"]


class ChatAISettings(models.Model):
    """Singleton: флаги ИИ в чатах (этап 1 — только copilot менеджера)."""

    copilot_enabled = models.BooleanField("Copilot менеджера", default=True)
    auto_reply_enabled = models.BooleanField("Автоответы клиенту", default=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_chat_ai_settings",
        verbose_name="Кто обновил",
    )

    class Meta:
        verbose_name = "Настройки ИИ чатов"
        verbose_name_plural = "Настройки ИИ чатов"

    def __str__(self) -> str:
        return "ИИ чатов"

    @classmethod
    def load(cls) -> "ChatAISettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ChatAISuggestion(models.Model):
    """Журнал черновиков ИИ: предложение → правки менеджера → исход."""

    CONF_HIGH = "high"
    CONF_MEDIUM = "medium"
    CONF_LOW = "low"
    CONF_CHOICES = [
        (CONF_HIGH, "Высокая"),
        (CONF_MEDIUM, "Средняя"),
        (CONF_LOW, "Низкая"),
    ]

    STATUS_DRAFT = "draft"
    STATUS_INSERTED = "inserted"
    STATUS_SENT = "sent"
    STATUS_REJECTED = "rejected"
    STATUS_EDITED = "edited"
    STATUS_ESCALATED = "escalated"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_INSERTED, "Вставлен"),
        (STATUS_SENT, "Отправлен"),
        (STATUS_REJECTED, "Отклонён"),
        (STATUS_EDITED, "Изменён"),
        (STATUS_ESCALATED, "Эскалация"),
        (STATUS_ERROR, "Ошибка"),
    ]

    FEEDBACK_USEFUL = "useful"
    FEEDBACK_PARTIAL = "partial"
    FEEDBACK_WRONG = "wrong"
    FEEDBACK_OUTDATED = "outdated"
    FEEDBACK_BAD_SOURCE = "bad_source"
    FEEDBACK_NO_AUTO = "no_auto"
    FEEDBACK_CHOICES = [
        (FEEDBACK_USEFUL, "Полезно"),
        (FEEDBACK_PARTIAL, "Частично полезно"),
        (FEEDBACK_WRONG, "Неверно"),
        (FEEDBACK_OUTDATED, "Устаревшая информация"),
        (FEEDBACK_BAD_SOURCE, "Не тот источник"),
        (FEEDBACK_NO_AUTO, "Нельзя отвечать автоматически"),
    ]

    thread = models.ForeignKey(
        ChatThread,
        on_delete=models.CASCADE,
        related_name="ai_suggestions",
        verbose_name="Тред",
    )
    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="chat_ai_suggestions",
        verbose_name="Клиент",
    )
    trigger_message = models.ForeignKey(
        ClientChatMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_suggestions_triggered",
        verbose_name="Сообщение-триггер",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chat_ai_suggestions_requested",
        verbose_name="Кто запросил",
    )
    prompt_context = models.JSONField("Контекст запроса", default=dict, blank=True)
    category = models.CharField("Категория", max_length=64, blank=True, default="")
    confidence = models.CharField(
        "Уверенность", max_length=16, choices=CONF_CHOICES, default=CONF_LOW
    )
    proposed_text = models.TextField("Предложенный текст", blank=True, default="")
    final_text = models.TextField("Итоговый текст менеджера", blank=True, default="")
    sources = models.JSONField("Источники", default=list, blank=True)
    warnings = models.JSONField("Предупреждения", default=list, blank=True)
    suggested_actions = models.JSONField("Действия", default=list, blank=True)
    needs_escalation = models.BooleanField("Нужна эскалация", default=False)
    auto_reply_allowed = models.BooleanField("Автоответ разрешён", default=False)
    status = models.CharField(
        "Статус", max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True
    )
    manager_feedback = models.CharField(
        "Оценка менеджера", max_length=32, choices=FEEDBACK_CHOICES, blank=True, default=""
    )
    feedback_note = models.TextField("Комментарий к оценке", blank=True, default="")
    model_name = models.CharField("Модель/провайдер", max_length=128, blank=True, default="stub")
    error_text = models.TextField("Ошибка", blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField("Закрыт", null=True, blank=True)

    class Meta:
        verbose_name = "Черновик ИИ чата"
        verbose_name_plural = "Черновики ИИ чатов"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["agency", "created_at"]),
            models.Index(fields=["thread", "status"]),
        ]


class ChatAIKnowledgeCandidate(models.Model):
    """Очередь модерации: кандидаты в базу знаний ИИ."""

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "На проверке"),
        (STATUS_APPROVED, "Утверждено"),
        (STATUS_REJECTED, "Отклонено"),
    ]

    suggestion = models.ForeignKey(
        ChatAISuggestion,
        on_delete=models.CASCADE,
        related_name="knowledge_candidates",
        verbose_name="Черновик ИИ",
        null=True,
        blank=True,
    )
    agency = models.ForeignKey(
        Agency,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chat_ai_kb_candidates",
        verbose_name="Клиент (если индивидуальный)",
    )
    question = models.TextField("Вопрос", blank=True, default="")
    answer = models.TextField("Ответ", blank=True, default="")
    category = models.CharField("Категория", max_length=64, blank=True, default="")
    client_visible = models.BooleanField("Доступно клиенту", default=False)
    status = models.CharField(
        "Статус", max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chat_ai_kb_candidates_created",
        verbose_name="Кто добавил",
    )
    moderated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chat_ai_kb_candidates_moderated",
        verbose_name="Модератор",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Кандидат в базу знаний ИИ"
        verbose_name_plural = "Кандидаты в базу знаний ИИ"
        ordering = ["-created_at", "-id"]


class AgencyPortalMember(models.Model):
    """Employee with access to a client personal cabinet (multi-user portal)."""

    ROLE_ADMIN = "admin"
    ROLE_MANAGER = "manager"
    ROLE_ACCOUNTANT = "accountant"
    ROLE_OPERATOR = "operator"
    ROLE_CUSTOM = "custom"
    ROLE_CHOICES = [
        (ROLE_ADMIN, "Администратор"),
        (ROLE_MANAGER, "Менеджер"),
        (ROLE_ACCOUNTANT, "Бухгалтер"),
        (ROLE_OPERATOR, "Оператор"),
        (ROLE_CUSTOM, "Свой доступ"),
    ]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="portal_members",
        verbose_name="Клиент",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agency_portal_memberships",
        verbose_name="Пользователь",
    )
    last_name = models.CharField("Фамилия", max_length=120)
    first_name = models.CharField("Имя", max_length=120)
    email = models.EmailField("Email")
    position = models.CharField("Должность", max_length=255, blank=True, default="")
    role = models.CharField("Роль", max_length=32, choices=ROLE_CHOICES, default=ROLE_CUSTOM)
    sections = models.JSONField("Разделы", default=list, blank=True)
    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_agency_portal_members",
        verbose_name="Кто добавил",
    )

    class Meta:
        verbose_name = "Сотрудник ЛК клиента"
        verbose_name_plural = "Сотрудники ЛК клиента"
        ordering = ["last_name", "first_name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["agency", "email"], name="uniq_agency_portal_member_email"),
        ]
        indexes = [
            models.Index(fields=["agency", "is_active"]),
            models.Index(fields=["user", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.email})"

    @property
    def full_name(self) -> str:
        return f"{self.last_name} {self.first_name}".strip()

    @property
    def initials(self) -> str:
        from .portal_access import initials_from_name

        return initials_from_name(self.last_name, self.first_name, self.email)
