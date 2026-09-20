import os
import re

from django.conf import settings
from django.db import models
from django.utils import timezone

from billing.models import BillingService
from sku.models import SKU


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-zА-Яа-яЁё0-9._-]+")


def goods_file_upload_to(instance, filename: str) -> str:
    original = os.path.basename(str(filename or "file"))
    stem, ext = os.path.splitext(original)
    safe_stem = _SAFE_FILENAME_RE.sub("_", stem).strip("._") or "file"
    safe_ext = re.sub(r"[^A-Za-z0-9.]", "", ext).lower()[:16]
    period = timezone.now().strftime("%Y/%m")
    return f"warehouse_goods/{instance.sku_id}/{period}/{safe_stem}{safe_ext}"


class GoodsProfile(models.Model):
    sku = models.OneToOneField(SKU, on_delete=models.CASCADE, related_name="warehouse_goods_profile")
    internal_notes = models.TextField("Внутренние заметки", blank=True)
    fbs_shelf_life_required = models.BooleanField("Контролировать срок годности FBS", default=False)
    receiving_task_template = models.TextField("Типовая задача на приемку", blank=True)
    shipping_task_template = models.TextField("Типовая задача на отгрузку", blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_goods_profiles",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Складской профиль товара"
        verbose_name_plural = "Складские профили товаров"

    def __str__(self):
        return f"Профиль {self.sku}"


class GoodsFile(models.Model):
    sku = models.ForeignKey(SKU, on_delete=models.CASCADE, related_name="warehouse_goods_files")
    file = models.FileField("Файл", upload_to=goods_file_upload_to)
    original_name = models.CharField("Имя файла", max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_goods_files",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "-id"]
        indexes = [models.Index(fields=["sku", "-uploaded_at"])]

    def __str__(self):
        return self.original_name


class GoodsExtraFieldDefinition(models.Model):
    TYPE_TEXT = "text"
    TYPE_NUMBER = "number"
    TYPE_BOOLEAN = "boolean"
    TYPE_DATE = "date"
    TYPE_CHOICES = [
        (TYPE_TEXT, "Текст"),
        (TYPE_NUMBER, "Число"),
        (TYPE_BOOLEAN, "Да/нет"),
        (TYPE_DATE, "Дата"),
    ]

    name = models.CharField("Название", max_length=128)
    slug = models.SlugField("Код", max_length=96, unique=True)
    field_type = models.CharField("Тип", max_length=16, choices=TYPE_CHOICES, default=TYPE_TEXT)
    is_active = models.BooleanField("Активно", default=True)
    sort_order = models.PositiveIntegerField("Порядок", default=100)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class GoodsExtraFieldValue(models.Model):
    sku = models.ForeignKey(SKU, on_delete=models.CASCADE, related_name="warehouse_goods_extra_values")
    definition = models.ForeignKey(
        GoodsExtraFieldDefinition,
        on_delete=models.CASCADE,
        related_name="values",
    )
    value = models.JSONField("Значение", null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_goods_extra_values",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["sku", "definition"], name="uniq_goods_extra_value")
        ]


class GoodsDefaultService(models.Model):
    STAGE_RECEIVING = "receiving"
    STAGE_PROCESSING = "processing"
    STAGE_SHIPPING = "shipping"
    STAGE_FULL_CYCLE = "full_cycle"
    STAGE_CHOICES = [
        (STAGE_RECEIVING, "Приемка"),
        (STAGE_PROCESSING, "Обработка"),
        (STAGE_SHIPPING, "Отгрузка"),
        (STAGE_FULL_CYCLE, "Полный цикл"),
    ]

    sku = models.ForeignKey(SKU, on_delete=models.CASCADE, related_name="warehouse_default_services")
    stage = models.CharField("Этап", max_length=24, choices=STAGE_CHOICES)
    service = models.ForeignKey(BillingService, on_delete=models.PROTECT, related_name="goods_defaults")
    unit_price = models.DecimalField("Цена", max_digits=14, decimal_places=4, null=True, blank=True)
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3, default=1)
    auto_add = models.BooleanField("Добавлять автоматически", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["stage", "service__sort_order", "service__name"]
        constraints = [
            models.UniqueConstraint(fields=["sku", "stage", "service"], name="uniq_goods_default_service")
        ]
        indexes = [models.Index(fields=["sku", "stage"])]

    def __str__(self):
        return f"{self.sku.sku_code}: {self.get_stage_display()} / {self.service.name}"


class GoodsActionAudit(models.Model):
    sku = models.ForeignKey(SKU, on_delete=models.CASCADE, related_name="warehouse_goods_actions")
    action = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_goods_actions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["sku", "-created_at"])]

