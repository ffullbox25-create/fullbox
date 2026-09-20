from django.conf import settings
from django.db import models

from sklad.models import WarehouseLocation
from sklad.topology import os_location_code
from sku.models import Agency, SKU


def inventory_location_label(location: WarehouseLocation) -> str:
    zone_code = str(location.zone_code or "").strip().upper()
    if zone_code == "OS" and all(
        (
            int(location.row_no or 0),
            int(location.section_no or 0),
            int(location.tier_no or 0),
            int(location.cell_no or 0),
        )
    ):
        return os_location_code(
            row=int(location.row_no),
            section=int(location.section_no),
            tier=int(location.tier_no),
            cell=int(location.cell_no),
        )
    return (
        str(location.display_name or "").strip()
        or str(location.location_code or "").strip()
        or zone_code
        or "—"
    )


class Inventory(models.Model):
    TYPE_FULL = "full"
    TYPE_PARTNER = "by_partner"
    TYPE_GOODS = "by_goods"
    TYPE_PLACES = "by_places"
    TYPE_CHOICES = [
        (TYPE_FULL, "Полная"),
        (TYPE_PARTNER, "По партнеру"),
        (TYPE_GOODS, "По товару"),
        (TYPE_PLACES, "По местам"),
    ]

    STATUS_CREATED = "created"
    STATUS_PENDING = "pending"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_CREATED, "Создана"),
        (STATUS_PENDING, "Передана в работу"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_COMPLETED, "Завершена"),
        (STATUS_CANCELED, "Отменена"),
    ]

    inventory_type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_CREATED)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventories",
    )
    sku = models.ForeignKey(
        SKU,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventories",
    )
    comment = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_inventories",
    )
    performed_by_name = models.CharField(max_length=255, blank=True)
    transferred_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inventory_inventory"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["inventory_type", "status"]),
            models.Index(fields=["agency", "status"]),
        ]
        verbose_name = "Инвентаризация"
        verbose_name_plural = "Инвентаризации"

    def __str__(self) -> str:
        return f"Инвентаризация №{self.pk or '-'}"


class InventoryLocation(models.Model):
    inventory = models.ForeignKey(
        Inventory,
        on_delete=models.CASCADE,
        related_name="scope_locations",
    )
    location = models.ForeignKey(
        WarehouseLocation,
        on_delete=models.PROTECT,
        related_name="inventory_scopes",
    )
    planned_qty = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_location"
        ordering = [
            "location__zone_code",
            "location__row_no",
            "location__section_no",
            "location__tier_no",
            "location__cell_no",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["inventory", "location"],
                name="uniq_inventory_location",
            )
        ]
        indexes = [models.Index(fields=["inventory", "location"])]
        verbose_name = "Место инвентаризации"
        verbose_name_plural = "Места инвентаризации"

    @property
    def display_code(self) -> str:
        return inventory_location_label(self.location)

    def __str__(self) -> str:
        return f"{self.inventory} · {self.display_code}"


class InventoryLine(models.Model):
    inventory = models.ForeignKey(
        Inventory,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    location = models.ForeignKey(
        WarehouseLocation,
        on_delete=models.PROTECT,
        related_name="inventory_lines",
    )
    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="inventory_lines",
    )
    sku_ref = models.ForeignKey(
        SKU,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_lines",
    )
    sku_code = models.CharField(max_length=64)
    name = models.CharField(max_length=255, blank=True)
    size = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    goods_type = models.CharField(max_length=64, blank=True)
    planned_qty = models.PositiveIntegerField(default=0)
    actual_qty = models.PositiveIntegerField(null=True, blank=True)
    source_snapshot_ids = models.JSONField(default=list, blank=True)
    counted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="counted_inventory_lines",
    )
    counted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inventory_line"
        ordering = ["location_id", "agency_id", "sku_code", "size", "barcode", "id"]
        indexes = [
            models.Index(fields=["inventory", "location"]),
            models.Index(fields=["inventory", "agency", "sku_code"]),
            models.Index(fields=["barcode"]),
        ]
        verbose_name = "Строка инвентаризации"
        verbose_name_plural = "Строки инвентаризации"

    @property
    def difference(self) -> int | None:
        if self.actual_qty is None:
            return None
        return int(self.actual_qty) - int(self.planned_qty)

    @property
    def has_discrepancy(self) -> bool:
        difference = self.difference
        return difference is not None and difference != 0

    @property
    def location_display(self) -> str:
        return inventory_location_label(self.location)

    def __str__(self) -> str:
        return f"{self.sku_code} · {self.location}"
