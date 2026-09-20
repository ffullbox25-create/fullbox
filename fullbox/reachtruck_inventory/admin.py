from django.contrib import admin

from .models import InventoryTask


@admin.register(InventoryTask)
class InventoryTaskAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "inventory",
        "location",
        "status",
        "assigned_to_name",
        "lease_expires_at",
        "planned_box_count",
        "actual_box_count",
        "discrepancy_reported_at",
        "created_at",
    )
    list_filter = ("status", "created_at", "discrepancy_reported_at")
    search_fields = (
        "inventory__id",
        "location__location_code",
        "assigned_to_name",
        "discrepancy_reported_by_name",
        "discrepancy_pallet_codes",
        "discrepancy_box_codes",
    )
    readonly_fields = (
        "last_activity_at",
        "lease_expires_at",
        "counted_at",
        "planned_box_count",
        "actual_box_count",
        "planned_box_codes",
        "planned_pallet_codes",
        "discrepancy_pallet_codes",
        "discrepancy_box_codes",
        "discrepancy_reported_by",
        "discrepancy_reported_by_name",
        "discrepancy_reported_at",
    )
