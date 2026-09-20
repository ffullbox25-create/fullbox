from django.db import models

from sku.models import Agency


class MarketSyncReport(models.Model):
    MARKETPLACE_CHOICES = [
        ("WB", "Wildberries"),
        ("OZON", "Ozon"),
    ]
    STATUS_CHOICES = [
        ("ok", "ok"),
        ("error", "error"),
    ]

    agency = models.ForeignKey(
        Agency, on_delete=models.CASCADE, related_name="market_sync_reports"
    )
    marketplace = models.CharField(max_length=16, choices=MARKETPLACE_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    duration_sec = models.FloatField(default=0)
    processed = models.PositiveIntegerField(default=0)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    barcodes_created = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-finished_at"]

    def __str__(self) -> str:
        return f"{self.marketplace} {self.agency_id} {self.finished_at:%Y-%m-%d %H:%M}"
