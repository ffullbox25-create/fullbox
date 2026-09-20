import os
import re
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from employees.models import Employee
from sku.models import Agency


_ATTACHMENT_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def processing_attachment_upload_to(instance, filename: str) -> str:
    original = os.path.basename(str(filename or "file"))
    stem, ext = os.path.splitext(original)
    safe_stem = _ATTACHMENT_FILENAME_RE.sub("_", stem).strip("._") or "file"
    safe_ext = _ATTACHMENT_FILENAME_RE.sub("", ext).lower()[:16]
    order_number = _ATTACHMENT_FILENAME_RE.sub("_", str(getattr(instance, "order_id", "") or "processing"))
    period = timezone.now().strftime("%Y/%m")
    return f"processing_attachments/{order_number}/{period}/{safe_stem}{safe_ext}"


class ProcessingPrintJob(models.Model):
    STATUS_PENDING = "pending"
    STATUS_PRINTING = "printing"
    STATUS_PRINTED = "printed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PRINTING, "Printing"),
        (STATUS_PRINTED, "Printed"),
        (STATUS_FAILED, "Failed"),
    ]

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    order_id = models.CharField(max_length=64, blank=True)
    card_id = models.CharField(max_length=128, blank=True)
    article = models.CharField(max_length=128, blank=True)
    barcode = models.CharField(max_length=128)
    size = models.CharField(max_length=64, blank=True)
    printer_name = models.CharField(max_length=255, blank=True)
    label_png_base64 = models.TextField(blank=True)
    label_png_base64_list = models.JSONField(default=list, blank=True)
    copies_count = models.PositiveIntegerField(default=1)
    template_key = models.CharField(max_length=64, blank=True)
    processing_param_key = models.CharField(max_length=64, blank=True)
    label_width_mm = models.PositiveIntegerField(default=58)
    label_height_mm = models.PositiveIntegerField(default=40)
    requested_by = models.CharField(max_length=150, blank=True)
    agent = models.CharField(max_length=128, blank=True)
    error = models.TextField(blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["status", "agent", "created_at"]),
            models.Index(fields=["agent", "status", "updated_at"]),
            models.Index(fields=["status", "lease_until"]),
        ]

    @classmethod
    def total_copies(cls, qs=None) -> int:
        queryset = qs if qs is not None else cls.objects.all()
        return int(queryset.aggregate(total=models.Sum("copies_count")).get("total") or 0)

    def __str__(self) -> str:
        return f"PrintJob #{self.pk} ({self.barcode})"


class ProcessingFlowSession(models.Model):
    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Открыта"),
        (STATUS_CLOSED, "Закрыта"),
    ]

    order_id = models.CharField(max_length=64)
    order_type = models.CharField(max_length=32, default="processing")
    agent_id = models.CharField(max_length=128, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_flow_sessions",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_flow_sessions",
    )
    flow_state = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    last_seen = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["order_id", "status"]),
            models.Index(fields=["agent_id", "status"]),
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self) -> str:
        label = self.agent_id or "agent"
        return f"FlowSession {self.order_id} ({label})"


class ProcessingWorkEvent(models.Model):
    TYPE_OPERATION_COMPLETED = "operation_completed"
    TYPE_BOX_FORMED = "box_formed"
    TYPE_CHOICES = [
        (TYPE_OPERATION_COMPLETED, "Операция обработки завершена"),
        (TYPE_BOX_FORMED, "Короб сформирован"),
    ]

    event_key = models.CharField(max_length=96, unique=True)
    operation_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    order_id = models.CharField(max_length=64, db_index=True)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_work_events",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_work_events",
    )
    employee_name = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_processing_work_events",
    )
    operation_key = models.CharField(max_length=128, blank=True)
    operation_label = models.CharField(max_length=255, blank=True)
    card_id = models.CharField(max_length=128, blank=True)
    container_code = models.CharField(max_length=128, blank=True)
    units = models.PositiveIntegerField(default=0)
    boxes = models.PositiveIntegerField(default=0)
    occurred_at = models.DateTimeField(db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["employee", "occurred_at"]),
            models.Index(fields=["operation_type", "occurred_at"]),
            models.Index(fields=["order_id", "operation_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_operation_type_display()}: {self.order_id}"


class ProcessingContainerCodeSequence(models.Model):
    agency = models.OneToOneField(
        Agency,
        on_delete=models.CASCADE,
        related_name="processing_container_code_sequence",
    )
    last_number = models.PositiveIntegerField(default=0)
    last_box_number = models.PositiveIntegerField(default=0)
    last_pallet_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Счетчик кодов коробов обработки"
        verbose_name_plural = "Счетчики кодов коробов обработки"

    def __str__(self) -> str:
        return f"{self.agency_id}: box={self.last_box_number}, pallet={self.last_pallet_number}"


class ProcessingOrderAttachment(models.Model):
    RETENTION_DAYS = 60

    order_id = models.CharField("Номер заявки", max_length=64, db_index=True)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="processing_order_attachments",
        verbose_name="Клиент",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_order_attachments",
        verbose_name="Кто загрузил",
    )
    file = models.FileField("Файл", upload_to=processing_attachment_upload_to)
    uploaded_at = models.DateTimeField("Загружен", auto_now_add=True)

    class Meta:
        verbose_name = "Файл заявки на обработку"
        verbose_name_plural = "Файлы заявок на обработку"
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

    @property
    def expires_at(self):
        return self.uploaded_at + timedelta(days=self.RETENTION_DAYS)

    @property
    def is_expired(self) -> bool:
        if not self.uploaded_at:
            return False
        return timezone.now() >= self.expires_at
