from django.contrib import admin

from . import models


class AllocationInline(admin.TabularInline):
    model = models.ReceivingDistributionAllocation
    extra = 0


class DirectionInline(admin.TabularInline):
    model = models.ReceivingDistributionDirection
    extra = 0
    show_change_link = True


class ItemInline(admin.TabularInline):
    model = models.ReceivingDistributionItem
    extra = 0


@admin.register(models.ReceivingDistributionPlan)
class ReceivingDistributionPlanAdmin(admin.ModelAdmin):
    list_display = ("receiving_order_id", "agency", "status", "expected_units", "created_at")
    list_filter = ("status",)
    search_fields = ("receiving_order_id", "agency__agn_name", "agency__short_name")
    inlines = [ItemInline, DirectionInline]


@admin.register(models.ReceivingDistributionDirection)
class ReceivingDistributionDirectionAdmin(admin.ModelAdmin):
    list_display = ("title", "plan", "kind", "status", "shipping_order", "expected_units")
    list_filter = ("kind", "status")
    search_fields = ("title", "plan__receiving_order_id", "supply_number")


@admin.register(models.ReceivingDistributionEvent)
class ReceivingDistributionEventAdmin(admin.ModelAdmin):
    list_display = ("plan", "action", "user", "created_at")
    list_filter = ("action",)
