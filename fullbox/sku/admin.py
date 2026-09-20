from django.contrib import admin

from .models import (
    Agency,
    Color,
    Market,
    MarketCredential,
    MarketplaceBinding,
    SKU,
    SKUBarcode,
    SKUPhoto,
    Store,
)
from audit.models import agency_snapshot, log_agency_change, log_sku_change, sku_snapshot


class SKUBarcodeInline(admin.TabularInline):
    model = SKUBarcode
    extra = 1


class SKUPhotoInline(admin.TabularInline):
    model = SKUPhoto
    extra = 1


class MarketplaceBindingInline(admin.TabularInline):
    model = MarketplaceBinding
    extra = 0


@admin.register(SKU)
class SKUAdmin(admin.ModelAdmin):
    list_display = (
        "sku_code",
        "name",
        "brand",
        "agency",
        "market",
        "color_ref",
        "size",
        "honest_sign",
        "use_nds",
        "deleted",
        "updated_at",
    )
    search_fields = ("sku_code", "name", "barcodes__value", "brand")
    list_filter = ("source", "honest_sign", "use_nds", "market", "color_ref", "deleted")
    inlines = [SKUBarcodeInline, SKUPhotoInline, MarketplaceBindingInline]
    ordering = ("sku_code",)

    def save_model(self, request, obj, form, change):
        action = "update" if change else "create"
        super().save_model(request, obj, form, change)
        log_sku_change(action, obj, user=request.user, description="Сохранение через админку")

    def delete_model(self, request, obj):
        snapshot = sku_snapshot(obj)
        log_sku_change("delete", obj, user=request.user, description="Удаление через админку", snapshot=snapshot)
        super().delete_model(request, obj)


@admin.register(SKUBarcode)
class SKUBarcodeAdmin(admin.ModelAdmin):
    list_display = ("value", "sku", "size", "is_primary")
    list_filter = ('is_primary',)
    search_fields = ('value', 'sku__sku_code', 'sku__name')


@admin.register(SKUPhoto)
class SKUPhotoAdmin(admin.ModelAdmin):
    list_display = ('sku', 'url', 'sort_order')
    ordering = ('sort_order',)


@admin.register(MarketplaceBinding)
class MarketplaceBindingAdmin(admin.ModelAdmin):
    list_display = ("sku", "marketplace", "external_id", "sync_mode", "last_synced_at")
    list_filter = ("marketplace", "sync_mode")
    search_fields = ("external_id", "sku__sku_code", "sku__name")


@admin.register(Market)
class MarketAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    ordering = ("id",)


@admin.register(Color)
class ColorAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    ordering = ("id",)


@admin.register(Agency)
class AgencyAdmin(admin.ModelAdmin):
    list_display = ("id", "agn_name", "short_name", "inn", "email", "phone", "use_nds")
    search_fields = ("agn_name", "short_name", "inn", "pref", "email")
    list_filter = ("use_nds",)

    def save_model(self, request, obj, form, change):
        action = "update" if change else "create"
        super().save_model(request, obj, form, change)
        log_agency_change(
            action,
            obj,
            user=request.user,
            description="Сохранение клиента через админку",
            snapshot=agency_snapshot(obj),
        )

    def delete_model(self, request, obj):
        snapshot = agency_snapshot(obj)
        log_agency_change(
            "delete",
            obj,
            user=request.user,
            description="Удаление клиента через админку",
            snapshot=snapshot,
        )
        super().delete_model(request, obj)


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("id", "stor_name", "agency")
    search_fields = ("stor_name", "agn_name")


@admin.register(MarketCredential)
class MarketCredentialAdmin(admin.ModelAdmin):
    list_display = ("id", "agency", "market")
    search_fields = ("agency__agn_name",)
