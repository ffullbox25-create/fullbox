from django.conf import settings
from django.db import models

from sklad.models import WarehouseOperation
from sku.models import Agency
from todo.models import Task


class BoxMoveOperation(models.Model):
    STATUS_SELECTING = "selecting"
    STATUS_DESTINATION = "destination"
    STATUS_VERIFYING = "verifying"
    STATUS_DONE = "done"
    STATUS_DONE_WITH_DISCREPANCY = "done_with_discrepancy"
    STATUS_CANCELED = "canceled"

    STATUS_CHOICES = [
        (STATUS_SELECTING, "Выбор коробов"),
        (STATUS_DESTINATION, "Выбор паллеты назначения"),
        (STATUS_VERIFYING, "Проверка паллеты назначения"),
        (STATUS_DONE, "Выполнено"),
        (STATUS_DONE_WITH_DISCREPANCY, "Выполнено с расхождениями"),
        (STATUS_CANCELED, "Отменено"),
    ]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reachtruck_box_move_operations",
    )
    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reachtruck_box_move_operations",
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_SELECTING)
    source_pallet_code = models.CharField(max_length=128, blank=True)
    source_location_label = models.CharField(max_length=255, blank=True)
    source_location_scan_code = models.CharField(max_length=64, blank=True)
    destination_pallet_code = models.CharField(max_length=128, blank=True)
    destination_location_label = models.CharField(max_length=255, blank=True)
    destination_location_scan_code = models.CharField(max_length=64, blank=True)
    selected_boxes = models.JSONField(default=list, blank=True)
    destination_expected_boxes = models.JSONField(default=list, blank=True)
    destination_scanned_boxes = models.JSONField(default=list, blank=True)
    discrepancies = models.JSONField(default=list, blank=True)
    warehouse_operation = models.ForeignKey(
        WarehouseOperation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="box_move_operations",
    )
    manager_task = models.ForeignKey(
        Task,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="box_move_operations",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "reachtruck_box_move_operation"
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["driver", "status"]),
            models.Index(fields=["source_pallet_code"]),
            models.Index(fields=["destination_pallet_code"]),
        ]

    def __str__(self) -> str:
        return f"Перемещение коробов #{self.pk or '-'}"
