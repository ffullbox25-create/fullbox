from django.contrib import admin

from .models import OtherRequestCategory


@admin.register(OtherRequestCategory)
class OtherRequestCategoryAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "code",
        "default_department",
        "default_sla_hours",
        "require_result_photo",
        "require_result_file",
        "require_qty",
        "can_be_paid",
        "is_active",
        "sort_order",
    )
    list_editable = (
        "default_department",
        "default_sla_hours",
        "require_result_photo",
        "require_result_file",
        "require_qty",
        "can_be_paid",
        "is_active",
        "sort_order",
    )
    search_fields = ("code", "title")
    ordering = ("sort_order", "title")
