import os
import re
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from sku.models import Agency


_ATTACHMENT_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def receiving_attachment_upload_to(instance, filename: str) -> str:
    original = os.path.basename(str(filename or "file"))
    stem, ext = os.path.splitext(original)
    safe_stem = _ATTACHMENT_FILENAME_RE.sub("_", stem).strip("._") or "file"
    safe_ext = _ATTACHMENT_FILENAME_RE.sub("", ext).lower()[:16]
    order_number = _ATTACHMENT_FILENAME_RE.sub("_", str(getattr(instance, "order_id", "") or "receiving"))
    period = timezone.now().strftime("%Y/%m")
    return f"receiving_attachments/{order_number}/{period}/{safe_stem}{safe_ext}"


class ReceivingOrderAttachment(models.Model):
    RETENTION_DAYS = 60

    order_id = models.CharField("Номер заявки", max_length=64, db_index=True)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="receiving_order_attachments",
        verbose_name="Клиент",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receiving_order_attachments",
        verbose_name="Кто загрузил",
    )
    file = models.FileField("Файл", upload_to=receiving_attachment_upload_to)
    uploaded_at = models.DateTimeField("Загружен", auto_now_add=True)

    class Meta:
        verbose_name = "Файл заявки на приемку"
        verbose_name_plural = "Файлы заявок на приемку"
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
