from django.conf import settings
from django.db import models

from inventory.models import Inventory, InventoryLocation, inventory_location_label
from sklad.models import WarehouseLocation


class InventoryTask(models.Model):
    STATUS_CREATED = "created"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_CREATED, "Ожидает исполнителя"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_COMPLETED, "Выполнено"),
        (STATUS_CANCELED, "Отменено"),
    ]

    inventory = models.ForeignKey(
        Inventory,
        on_delete=models.CASCADE,
        related_name="execution_tasks",
    )
    scope_location = models.OneToOneField(
        InventoryLocation,
        on_delete=models.CASCADE,
        related_name="execution_task",
    )
    location = models.ForeignKey(
        WarehouseLocation,
        on_delete=models.PROTECT,
        related_name="reachtruck_inventory_tasks",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_CREATED)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reachtruck_inventory_tasks",
    )
    assigned_to_name = models.CharField(max_length=255, blank=True)
    location_verified_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    counted_at = models.DateTimeField(null=True, blank=True)
    planned_box_count = models.PositiveIntegerField(default=0)
    actual_box_count = models.PositiveIntegerField(null=True, blank=True)
    planned_box_codes = models.JSONField(default=list, blank=True)
    planned_pallet_codes = models.JSONField(default=list, blank=True)
    discrepancy_pallet_codes = models.JSONField(default=list, blank=True)
    discrepancy_box_codes = models.JSONField(default=list, blank=True)
    discrepancy_reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reported_inventory_discrepancies",
    )
    discrepancy_reported_by_name = models.CharField(max_length=255, blank=True)
    discrepancy_reported_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "reachtruck_inventory_task"
        ordering = ["status", "created_at", "id"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["assigned_to", "status"]),
            models.Index(fields=["inventory", "status"]),
            models.Index(fields=["location", "status"]),
            models.Index(
                fields=["status", "lease_expires_at"],
                name="rt_inv_status_lease_idx",
            ),
        ]
        verbose_name = "Задание ричтракеру на инвентаризацию"
        verbose_name_plural = "Задания ричтрактерам на инвентаризацию"

    @property
    def location_code(self) -> str:
        return inventory_location_label(self.location)

    @property
    def location_parts(self) -> list[dict[str, str]]:
        return [{"label": "Место", "value": self.location_code}]

    @property
    def box_difference(self) -> int | None:
        if self.actual_box_count is None:
            return None
        return int(self.actual_box_count) - int(self.planned_box_count)

    def __str__(self) -> str:
        return f"Инвентаризация №{self.inventory_id} · {self.location_code}"
