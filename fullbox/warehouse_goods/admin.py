from django.contrib import admin

from .models import (
    GoodsActionAudit,
    GoodsDefaultService,
    GoodsExtraFieldDefinition,
    GoodsExtraFieldValue,
    GoodsFile,
    GoodsProfile,
)


@admin.register(GoodsProfile)
class GoodsProfileAdmin(admin.ModelAdmin):
    list_display = ("sku", "fbs_shelf_life_required", "updated_at", "updated_by")
    search_fields = ("sku__sku_code", "sku__name")


admin.site.register(GoodsFile)
admin.site.register(GoodsExtraFieldDefinition)
admin.site.register(GoodsExtraFieldValue)
admin.site.register(GoodsDefaultService)
admin.site.register(GoodsActionAudit)
