from django.contrib import admin

from .models import ShippingOrder, ShippingOrderAttachment, ShippingOrderItem, ShippingReserve, ShippingTransportNote


class ShippingOrderItemInline(admin.TabularInline):
    model = ShippingOrderItem
    extra = 0


class ShippingOrderAttachmentInline(admin.TabularInline):
    model = ShippingOrderAttachment
    extra = 0


@admin.register(ShippingOrder)
class ShippingOrderAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "agency",
        "status",
        "delivery_type",
        "planned_ship_date",
        "created_at",
    )
    list_filter = ("status", "delivery_type", "agency")
    search_fields = ("number", "agency__agn_name", "agency__inn")
    inlines = [ShippingOrderItemInline, ShippingOrderAttachmentInline]


@admin.register(ShippingReserve)
class ShippingReserveAdmin(admin.ModelAdmin):
    list_display = ("order", "item", "agency", "sku_code", "size", "qty", "created_at")
    list_filter = ("agency", "goods_type")
    search_fields = ("order__number", "sku_code", "barcode")


@admin.register(ShippingOrderAttachment)
class ShippingOrderAttachmentAdmin(admin.ModelAdmin):
    list_display = ("order", "filename", "uploaded_by", "uploaded_at")
    list_filter = ("uploaded_at",)
    search_fields = ("order__number", "file")

    @admin.display(description="Файл")
    def filename(self, obj):
        return obj.filename


@admin.register(ShippingTransportNote)
class ShippingTransportNoteAdmin(admin.ModelAdmin):
    list_display = ("order", "document_number", "document_date", "carrier_name", "updated_at")
    list_filter = ("document_date", "updated_at")
    search_fields = ("order__number", "document_number", "shipper_name", "consignee_name", "carrier_name")
