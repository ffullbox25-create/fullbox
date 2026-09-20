"""
Расширение приёмки: план распределения по направлениям.

Головная приёмка остаётся в audit.OrderAuditEntry (order_type=receiving).
Складская логика, короба, ШК, печать, остатки — не затрагиваются.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from sku.models import Agency, Market, SKU


def distribution_source_upload_to(instance, filename: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (filename or "file.xlsx"))[:120]
    agency_id = getattr(instance, "agency_id", None) or "0"
    return f"receiving_distribution/{agency_id}/{instance.pk or 'new'}_{safe}"


class ReceivingDistributionPlan(models.Model):
    """План распределения, привязанный к существующей заявке PR-…"""

    STATUS_DRAFT = "draft"
    STATUS_SUBMITTED = "submitted"
    STATUS_RETURNED = "returned"
    STATUS_CONFIRMED = "confirmed"
    STATUS_AWAITING_RECEIVING = "awaiting_receiving"
    STATUS_RECEIVING = "receiving"
    STATUS_PARTIAL = "partial"
    STATUS_DONE = "done"
    STATUS_DISCREPANCY = "discrepancy"
    STATUS_CLOSED = "closed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_SUBMITTED, "Отправлена"),
        (STATUS_RETURNED, "Возвращена на исправление"),
        (STATUS_CONFIRMED, "Подтверждена"),
        (STATUS_AWAITING_RECEIVING, "Ожидает приемки"),
        (STATUS_RECEIVING, "Приемка начата"),
        (STATUS_PARTIAL, "Принята частично"),
        (STATUS_DONE, "Принята полностью"),
        (STATUS_DISCREPANCY, "Есть расхождения"),
        (STATUS_CLOSED, "Закрыта"),
        (STATUS_CANCELLED, "Отменена"),
    ]

    receiving_order_id = models.CharField(
        "Номер головной приёмки",
        max_length=64,
        unique=True,
        db_index=True,
        help_text="PR-… из audit.OrderAuditEntry, без отдельной модели приёмки",
    )
    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="receiving_distribution_plans",
        verbose_name="Клиент",
    )
    status = models.CharField(
        "Статус плана",
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
        db_index=True,
    )
    client_request_id = models.CharField(
        "Идемпотентный ключ формы",
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Защита от повторной отправки формы",
    )
    comment = models.TextField("Комментарий", blank=True, default="")
    eta_at = models.DateTimeField("Плановое прибытие", null=True, blank=True)
    expected_units = models.PositiveIntegerField("Заявлено единиц", default=0)
    expected_boxes = models.PositiveIntegerField("Примерно коробов", default=0)
    expected_pallets = models.PositiveIntegerField("Примерно палет", default=0)
    vehicle_number = models.CharField("Машина", max_length=64, blank=True, default="")
    driver_name = models.CharField("Водитель", max_length=128, blank=True, default="")
    driver_phone = models.CharField("Телефон", max_length=64, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_receiving_distribution_plans",
        verbose_name="Создал",
    )
    submitted_at = models.DateTimeField("Отправлена", null=True, blank=True)
    source_file = models.FileField(
        "Исходный Excel",
        upload_to=distribution_source_upload_to,
        blank=True,
        null=True,
        max_length=512,
    )
    source_filename = models.CharField("Имя файла", max_length=255, blank=True, default="")
    check_status = models.CharField(
        "Статус проверки файла",
        max_length=32,
        blank=True,
        default="",
        help_text="empty|uploaded|checking|ok|errors|warnings|stale",
    )
    check_payload = models.JSONField("Результат проверки", default=dict, blank=True)
    checked_at = models.DateTimeField("Проверено", null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_receiving_distribution_plans",
        verbose_name="Изменил",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "План приёмки с распределением"
        verbose_name_plural = "Планы приёмки с распределением"
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "client_request_id"],
                condition=~models.Q(client_request_id=""),
                name="uniq_rd_plan_agency_client_request",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.receiving_order_id} · {self.get_status_display()}"


class ReceivingDistributionDirection(models.Model):
    """Направление внутри плана. Хранение FullBox — без заявки на отгрузку."""

    KIND_MARKETPLACE = "marketplace"
    KIND_STORAGE = "storage_fullbox"
    KIND_OTHER = "other"
    KIND_CHOICES = [
        (KIND_MARKETPLACE, "Маркетплейс"),
        (KIND_STORAGE, "Хранение FullBox"),
        (KIND_OTHER, "Другое"),
    ]

    STATUS_AWAITING = "awaiting_receiving"
    STATUS_RECEIVING = "receiving"
    STATUS_PARTIAL = "partial"
    STATUS_DONE = "done"
    STATUS_DISCREPANCY = "discrepancy"
    STATUS_READY = "ready_to_ship"
    STATUS_LOGISTICS = "in_logistics"
    STATUS_SHIPPED = "shipped"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_AWAITING, "Ожидает приемки"),
        (STATUS_RECEIVING, "Принимается"),
        (STATUS_PARTIAL, "Принято частично"),
        (STATUS_DONE, "Принято полностью"),
        (STATUS_DISCREPANCY, "Есть расхождения"),
        (STATUS_READY, "Готово к отгрузке"),
        (STATUS_LOGISTICS, "Передано логисту"),
        (STATUS_SHIPPED, "Отгружено"),
        (STATUS_CANCELLED, "Отменено"),
    ]

    plan = models.ForeignKey(
        ReceivingDistributionPlan,
        on_delete=models.CASCADE,
        related_name="directions",
        verbose_name="План",
    )
    sort_order = models.PositiveIntegerField("Порядок", default=0)
    kind = models.CharField("Тип", max_length=32, choices=KIND_CHOICES, default=KIND_MARKETPLACE)
    title = models.CharField("Название направления", max_length=255, blank=True, default="")
    marketplace = models.ForeignKey(
        Market,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receiving_distribution_directions",
        verbose_name="Маркетплейс",
    )
    marketplace_name = models.CharField("Маркетплейс (текст)", max_length=64, blank=True, default="")
    destination_warehouse = models.CharField("Склад назначения", max_length=255, blank=True, default="")
    cluster = models.CharField("Кластер", max_length=128, blank=True, default="")
    supply_number = models.CharField("Номер поставки", max_length=128, blank=True, default="")
    slot_date = models.DateField("Дата поставки", null=True, blank=True)
    slot_time = models.CharField("Таймслот", max_length=64, blank=True, default="")
    delivery_type = models.CharField("Способ доставки", max_length=32, blank=True, default="")
    expected_units = models.PositiveIntegerField("Заявлено единиц", default=0)
    expected_boxes = models.PositiveIntegerField("Коробов", default=0)
    expected_pallets = models.PositiveIntegerField("Палет", default=0)
    weight_kg = models.DecimalField("Вес, кг", max_digits=12, decimal_places=3, null=True, blank=True)
    volume_m3 = models.DecimalField("Объём, м³", max_digits=12, decimal_places=3, null=True, blank=True)
    comment = models.TextField("Комментарий", blank=True, default="")
    status = models.CharField(
        "Статус направления",
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_AWAITING,
        db_index=True,
    )
    shipping_order = models.ForeignKey(
        "shipping.ShippingOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receiving_distribution_directions",
        verbose_name="Связанная отгрузка",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Направление распределения"
        verbose_name_plural = "Направления распределения"
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return self.title or self.destination_warehouse or self.get_kind_display()

    @property
    def needs_shipping(self) -> bool:
        return self.kind != self.KIND_STORAGE


class ReceivingDistributionItem(models.Model):
    """Строка номенклатуры головной приёмки (общее количество)."""

    plan = models.ForeignKey(
        ReceivingDistributionPlan,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="План",
    )
    sku = models.ForeignKey(
        SKU,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receiving_distribution_items",
        verbose_name="SKU",
    )
    sku_code = models.CharField("Артикул", max_length=128, db_index=True)
    barcode = models.CharField("Штрихкод", max_length=128, blank=True, default="")
    name = models.CharField("Наименование", max_length=512, blank=True, default="")
    size = models.CharField("Размер / характеристика", max_length=128, blank=True, default="")
    qty_total = models.PositiveIntegerField("Общее количество")
    comment = models.CharField("Комментарий", max_length=512, blank=True, default="")

    class Meta:
        verbose_name = "Товар плана распределения"
        verbose_name_plural = "Товары плана распределения"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "sku_code"], name="uniq_rd_item_plan_sku"),
        ]

    def __str__(self) -> str:
        return f"{self.sku_code} × {self.qty_total}"


class ReceivingDistributionAllocation(models.Model):
    """Количество артикула на конкретное направление."""

    plan = models.ForeignKey(
        ReceivingDistributionPlan,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name="План",
    )
    item = models.ForeignKey(
        ReceivingDistributionItem,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name="Товар",
    )
    direction = models.ForeignKey(
        ReceivingDistributionDirection,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name="Направление",
    )
    qty = models.PositiveIntegerField("Количество по направлению")
    qty_accepted = models.PositiveIntegerField(
        "Фактически принято по направлению",
        default=0,
        help_text="Заполняется хуком из текущей приёмки; не является отдельным складским регистром",
    )

    class Meta:
        verbose_name = "Распределение по направлению"
        verbose_name_plural = "Распределения по направлениям"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "direction"],
                name="uniq_rd_alloc_item_direction",
            ),
            models.CheckConstraint(condition=models.Q(qty__gt=0), name="rd_alloc_qty_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.item.sku_code} → {self.direction_id}: {self.qty}"


class ReceivingDistributionEvent(models.Model):
    """Аудит действий нового процесса (не история коробов/ШК)."""

    plan = models.ForeignKey(
        ReceivingDistributionPlan,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name="План",
    )
    action = models.CharField("Действие", max_length=64, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receiving_distribution_events",
    )
    role = models.CharField("Роль", max_length=64, blank=True, default="")
    object_type = models.CharField("Тип объекта", max_length=64, blank=True, default="")
    object_id = models.CharField("ID объекта", max_length=64, blank=True, default="")
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    reason = models.CharField("Причина", max_length=512, blank=True, default="")
    source = models.CharField("Источник", max_length=64, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "Событие распределения"
        verbose_name_plural = "События распределения"
        ordering = ["-id"]
