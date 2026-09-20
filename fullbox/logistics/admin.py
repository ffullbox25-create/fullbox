from django.contrib import admin

from .models import (
    LogisticsTrip,
    LogisticsTripOrder,
    ProblemTrip,
    TripAttachment,
    TripHistory,
    TripNotification,
    TripPenalty,
)


class LogisticsTripOrderInline(admin.TabularInline):
    model = LogisticsTripOrder
    extra = 0


@admin.register(LogisticsTrip)
class LogisticsTripAdmin(admin.ModelAdmin):
    list_display = ("number", "trip_date", "status", "driver_status", "assigned_driver", "assigned_logistician", "vehicle_name", "vehicle_number", "created_at")
    list_filter = ("status", "driver_status", "trip_date", "vehicle_type")
    search_fields = ("number", "vehicle_name", "vehicle_number", "driver_name")
    inlines = [LogisticsTripOrderInline]


@admin.register(LogisticsTripOrder)
class LogisticsTripOrderAdmin(admin.ModelAdmin):
    list_display = ("trip", "shipping_order", "loading_sequence", "delivery_sequence", "updated_at")
    list_filter = ("trip__status",)
    search_fields = ("trip__number", "shipping_order__number", "shipping_order__agency__agn_name")


@admin.register(ProblemTrip)
class ProblemTripAdmin(admin.ModelAdmin):
    list_display = ("trip", "reason_code", "status", "responsible_party", "assigned_to", "created_at")
    list_filter = ("status", "reason_code", "responsible_party")
    search_fields = ("trip__number", "comment", "support_case_number")


@admin.register(TripPenalty)
class TripPenaltyAdmin(admin.ModelAdmin):
    list_display = ("problem", "marketplace_name", "penalty_type", "estimated_amount", "actual_amount", "status")
    list_filter = ("status", "responsible_party", "marketplace_name")
    search_fields = ("problem__trip__number", "penalty_type", "document_number", "support_reference")


@admin.register(TripAttachment)
class TripAttachmentAdmin(admin.ModelAdmin):
    list_display = ("trip", "attachment_type", "uploaded_by", "created_at")
    list_filter = ("attachment_type", "created_at")
    search_fields = ("trip__number", "file")


@admin.register(TripNotification)
class TripNotificationAdmin(admin.ModelAdmin):
    list_display = ("recipient", "trip", "title", "read_at", "created_at")
    list_filter = ("read_at", "created_at")
    search_fields = ("recipient__full_name", "trip__number", "title", "text")


@admin.register(TripHistory)
class TripHistoryAdmin(admin.ModelAdmin):
    list_display = ("trip", "event_type", "description", "actor", "created_at")
    list_filter = ("event_type", "created_at")
    search_fields = ("trip__number", "description")
    readonly_fields = ("trip", "event_type", "description", "actor", "payload", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
