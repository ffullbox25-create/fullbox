from __future__ import annotations

from django.conf import settings
from django.db import models


class OtgDeliveryRequest(models.Model):
    STATUS_REQUESTED = "requested"
    STATUS_PLANNING = "planning"
    STATUS_PLANNED = "planned"
    STATUS_DISPATCHED = "dispatched"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_PARTIAL = "partial"
    STATUS_DONE = "done"
    STATUS_BLOCKED = "blocked"
    STATUS_CANCELED = "canceled"

    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Requested"),
        (STATUS_PLANNING, "Planning"),
        (STATUS_PLANNED, "Planned"),
        (STATUS_DISPATCHED, "Dispatched"),
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_PARTIAL, "Partial"),
        (STATUS_DONE, "Done"),
        (STATUS_BLOCKED, "Blocked"),
        (STATUS_CANCELED, "Canceled"),
    ]

    shipping_order = models.ForeignKey(
        "shipping.ShippingOrder",
        on_delete=models.CASCADE,
        related_name="otg_delivery_requests",
    )
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="otg_delivery_requests",
    )
    move_request = models.ForeignKey(
        "reachtruck.MoveRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="otg_delivery_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="otg_delivery_requests",
    )
    requested_by_name = models.CharField(max_length=255, blank=True)
    requested_by_role = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_REQUESTED)
    requested_boxes = models.PositiveIntegerField(default=0)
    planned_boxes = models.PositiveIntegerField(default=0)
    shortage_boxes = models.PositiveIntegerField(default=0)
    planning_error = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["shipping_order", "status"]),
            models.Index(fields=["agency", "status"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"OTG {self.shipping_order_id} ({self.status})"


class OtgDeliveryDemand(models.Model):
    TYPE_FULL_BOX = "full_box"
    TYPE_MIXED_BOX = "mixed_box"
    TYPE_PARTIAL_BOX_SPLIT = "partial_box_split"

    TYPE_CHOICES = [
        (TYPE_FULL_BOX, "Full box"),
        (TYPE_MIXED_BOX, "Mixed box"),
        (TYPE_PARTIAL_BOX_SPLIT, "Partial box split"),
    ]

    request = models.ForeignKey(
        OtgDeliveryRequest,
        on_delete=models.CASCADE,
        related_name="demands",
    )
    demand_key = models.CharField(max_length=64)
    demand_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    boxes_required = models.PositiveIntegerField(default=0)
    boxes_planned = models.PositiveIntegerField(default=0)
    box_qty = models.PositiveIntegerField(default=0)
    composition = models.JSONField(default=list, blank=True)
    pick_composition = models.JSONField(default=list, blank=True)
    item_quantities_per_box = models.JSONField(default=dict, blank=True)
    source_box_codes = models.JSONField(default=list, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["request", "demand_key"], name="uniq_otg_delivery_demand_key"),
        ]
        indexes = [
            models.Index(fields=["request", "demand_type"]),
            models.Index(fields=["demand_key"]),
        ]
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.demand_type}: {self.boxes_required}"


class OtgPalletPlan(models.Model):
    TYPE_FULL_PALLET = "full_pallet"
    TYPE_PICK_BOXES = "pick_boxes"
    TYPE_PARTIAL_BOX_SPLIT = "partial_box_split"

    TYPE_CHOICES = [
        (TYPE_FULL_PALLET, "Full pallet"),
        (TYPE_PICK_BOXES, "Pick boxes"),
        (TYPE_PARTIAL_BOX_SPLIT, "Partial box split"),
    ]

    request = models.ForeignKey(
        OtgDeliveryRequest,
        on_delete=models.CASCADE,
        related_name="pallet_plans",
    )
    demand = models.ForeignKey(
        OtgDeliveryDemand,
        on_delete=models.CASCADE,
        related_name="pallet_plans",
    )
    move_task = models.ForeignKey(
        "reachtruck.MoveTask",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="otg_pallet_plans",
    )
    plan_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    pallet_code = models.CharField(max_length=128, db_index=True)
    boxes_planned = models.PositiveIntegerField(default=0)
    qty_planned = models.PositiveIntegerField(default=0)
    from_location = models.JSONField(default=dict, blank=True)
    selection_mode = models.CharField(max_length=32, blank=True)
    planned_box_codes = models.JSONField(default=list, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["request", "plan_type"]),
            models.Index(fields=["pallet_code", "plan_type"]),
        ]
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.plan_type}: {self.pallet_code}"


class OtgPlanningEvent(models.Model):
    request = models.ForeignKey(
        OtgDeliveryRequest,
        on_delete=models.CASCADE,
        related_name="events",
    )
    event_type = models.CharField(max_length=64)
    message = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["request", "event_type"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["id"]
