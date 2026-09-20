from django.conf import settings
from django.db import models

from sku.models import Agency, SKU


class ProcessingCzUnit(models.Model):
    order_id = models.CharField(max_length=64)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_cz_units",
    )
    sku = models.ForeignKey(
        SKU,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_cz_units",
    )
    sku_code = models.CharField(max_length=64)
    name = models.CharField(max_length=255, blank=True)
    size = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=128)
    marking_code = models.TextField(unique=True)
    box_code = models.CharField(max_length=128)
    pallet_code = models.CharField(max_length=128, blank=True)
    accepted_at = models.DateTimeField(auto_now_add=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_cz_units",
    )

    class Meta:
        db_table = "processing_cz_unit"
        ordering = ["accepted_at", "id"]
        indexes = [
            models.Index(fields=["order_id", "sku_code", "size"], name="pcz_unit_order_sku_size_idx"),
            models.Index(fields=["order_id", "box_code"], name="pcz_unit_order_box_idx"),
            models.Index(fields=["order_id", "pallet_code"], name="pcz_unit_order_pallet_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.order_id} | {self.sku_code} | {self.marking_code}"
