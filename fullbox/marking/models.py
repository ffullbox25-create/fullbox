from django.conf import settings
from django.db import models

from sku.models import Agency, SKU

from .codes import marking_code_identity, normalize_marking_code


LEGACY_IDENTITY_SEPARATOR = "\x1flegacy:"


class MarkingCodeQuerySet(models.QuerySet):
    def bulk_create(self, objs, *args, **kwargs):
        for obj in objs:
            obj.prepare_identifiers()
        return super().bulk_create(objs, *args, **kwargs)


class MarkingCode(models.Model):
    ORDER_TYPE_CHOICES = [
        ("processing", "Обработка"),
        ("receiving", "Приемка"),
        ("placement", "Размещение"),
        ("shipping", "Отгрузка"),
        ("other", "Прочее"),
    ]
    SOURCE_CHOICES = [
        ("scan", "Сканер"),
        ("import", "Импорт"),
    ]

    order_type = models.CharField(max_length=32, choices=ORDER_TYPE_CHOICES, default="processing")
    order_id = models.CharField(max_length=64, blank=True)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="marking_codes",
        verbose_name="Клиент",
    )
    sku = models.ForeignKey(
        SKU,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="marking_codes",
        verbose_name="SKU",
    )
    sku_code = models.CharField("Артикул", max_length=64)
    size = models.CharField("Размер", max_length=64, blank=True)
    barcode = models.CharField("Штрихкод", max_length=128, blank=True)
    box_barcode = models.CharField("Штрихкод короба", max_length=128, blank=True)
    code = models.TextField("Код ЧЗ", unique=True)
    identity_key = models.TextField(
        "Идентификатор единицы ЧЗ", blank=True, default="", editable=False
    )
    source = models.CharField("Источник", max_length=16, choices=SOURCE_CHOICES, default="scan")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="marking_codes",
        verbose_name="Пользователь",
    )
    used_at = models.DateTimeField("Использован", null=True, blank=True)
    used_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="used_marking_codes",
        verbose_name="Использовал",
    )
    printed_at = models.DateTimeField("Напечатан", null=True, blank=True)
    printed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="printed_marking_codes",
        verbose_name="Напечатал",
    )
    print_job_id = models.PositiveBigIntegerField(
        "Задание печати",
        null=True,
        blank=True,
        db_index=True,
    )
    print_reserved_at = models.DateTimeField("Передан в очередь печати", null=True, blank=True)

    objects = MarkingCodeQuerySet.as_manager()

    class Meta:
        verbose_name = "Код ЧЗ"
        verbose_name_plural = "Коды ЧЗ"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order_type", "order_id"]),
            models.Index(fields=["sku_code", "size"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["identity_key"],
                condition=~models.Q(identity_key=""),
                name="uniq_marking_code_identity",
            ),
        ]

    def prepare_identifiers(self) -> None:
        previous_identity = self.identity_key
        self.code = normalize_marking_code(self.code)
        canonical_identity = marking_code_identity(self.code)
        legacy_prefix = f"{canonical_identity}{LEGACY_IDENTITY_SEPARATOR}"
        if self.pk and previous_identity.startswith(legacy_prefix):
            # Historical duplicates are retained for audit and get a unique
            # legacy key during migration. Keep that key on later status edits.
            self.identity_key = previous_identity
        else:
            self.identity_key = canonical_identity

    def save(self, *args, **kwargs):
        previous_code = self.code
        previous_identity = self.identity_key
        self.prepare_identifiers()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            if self.code != previous_code or "code" in update_fields:
                update_fields.add("code")
            if self.identity_key != previous_identity or "code" in update_fields:
                update_fields.add("identity_key")
            kwargs["update_fields"] = update_fields
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.code} ({self.sku_code} {self.size})"
