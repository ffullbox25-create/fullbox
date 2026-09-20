from django.contrib import admin

from .models import AuditEntry, AuditJournal, OrderAuditEntry


@admin.register(AuditJournal)
class AuditJournalAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "description")
    search_fields = ("code", "name", "description")
    ordering = ("code",)


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "journal", "action", "sku", "agency", "user", "description")
    list_filter = ("action", "user", "journal")
    search_fields = (
        "sku__sku_code",
        "sku__name",
        "agency__agn_name",
        "agency__inn",
        "description",
        "journal__name",
        "journal__code",
    )
    ordering = ("-created_at",)


@admin.register(OrderAuditEntry)
class OrderAuditEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "order_type", "order_id", "action", "agency", "user", "description")
    list_filter = ("action", "order_type", "agency")
    search_fields = ("order_id", "description", "agency__agn_name")
    ordering = ("-created_at",)
