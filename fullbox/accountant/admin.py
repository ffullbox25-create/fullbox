from django.contrib import admin

from .models import ClientChangeLog, ClientLifecycle


@admin.register(ClientLifecycle)
class ClientLifecycleAdmin(admin.ModelAdmin):
    list_display = ("agency", "status", "client_type", "serving_company", "updated_at")
    list_filter = ("status", "client_type")
    search_fields = ("agency__agn_name", "agency__inn", "agency__short_name")
    raw_id_fields = ("agency", "serving_company", "created_by", "activated_by")


@admin.register(ClientChangeLog)
class ClientChangeLogAdmin(admin.ModelAdmin):
    list_display = ("agency", "field_name", "user", "changed_at")
    search_fields = ("agency__agn_name", "field_name", "comment")
    raw_id_fields = ("agency", "lifecycle", "user")
