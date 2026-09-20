from django.conf import settings
from django.db import models

from sku.models import Agency, SKU


class ReceivingCzUnit(models.Model):
    order_id = models.CharField(max_length=64)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receiving_cz_units",
    )
    sku = models.ForeignKey(
        SKU,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receiving_cz_units",
    )
    sku_code = models.CharField(max_length=64)
    name = models.CharField(max_length=255, blank=True)
    size = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=128)
    marking_code = models.TextField()
    source_shipping_order_number = models.CharField(
        max_length=32,
        blank=True,
        default="",
        db_index=True,
    )
    source_mark_verified = models.BooleanField(default=True)
    source_discrepancy_reason = models.CharField(max_length=64, blank=True, default="")
    box_code = models.CharField(max_length=128)
    pallet_code = models.CharField(max_length=128, blank=True)
    accepted_at = models.DateTimeField(auto_now_add=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receiving_cz_units",
    )

    class Meta:
        db_table = "receiving_cz_unit"
        ordering = ["accepted_at", "id"]
        indexes = [
            models.Index(fields=["order_id", "sku_code", "size"]),
            models.Index(fields=["order_id", "box_code"]),
            models.Index(fields=["order_id", "pallet_code"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["order_id", "marking_code"],
                name="uniq_receiving_cz_order_mark",
            ),
            models.UniqueConstraint(
                fields=["source_shipping_order_number", "marking_code"],
                condition=~models.Q(source_shipping_order_number=""),
                name="uniq_receiving_cz_source_mark",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.order_id} · {self.sku_code} · {self.marking_code}"
