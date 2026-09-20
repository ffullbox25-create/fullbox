from django.contrib import admin

from .models import MarkingCode


@admin.register(MarkingCode)
class MarkingCodeAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "sku_code",
        "size",
        "order_type",
        "order_id",
        "barcode",
        "box_barcode",
        "agency",
        "created_at",
    )
    list_filter = ("order_type", "source", "agency")
    search_fields = ("code", "sku_code", "barcode", "order_id")
