from django.contrib import admin

from .models import Inventory, InventoryLine, InventoryLocation


class InventoryLocationInline(admin.TabularInline):
    model = InventoryLocation
    extra = 0


class InventoryLineInline(admin.TabularInline):
    model = InventoryLine
    extra = 0
    readonly_fields = ("planned_qty", "actual_qty", "counted_by", "counted_at")


@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ("id", "inventory_type", "status", "agency", "created_by", "created_at")
    list_filter = ("inventory_type", "status", "created_at")
    search_fields = ("id", "agency__agn_name", "sku__sku_code", "comment")
    inlines = (InventoryLocationInline, InventoryLineInline)

