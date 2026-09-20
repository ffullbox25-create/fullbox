from django.contrib import admin

from .models import Employee, EmployeeBadge, EmployeeBadgeEvent


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        'full_name',
        'role',
        'access_roles',
        'email',
        'phone',
        'is_active',
        'qr_login_enabled',
        'updated_at',
    )
    list_filter = ('role', 'is_active', 'qr_login_enabled')
    search_fields = ('full_name', 'email', 'phone')
    ordering = ('full_name',)
    readonly_fields = ('qr_login_enabled',)


@admin.register(EmployeeBadge)
class EmployeeBadgeAdmin(admin.ModelAdmin):
    list_display = ('employee', 'issued_at', 'issued_by', 'revoked_at', 'last_used_at', 'last_used_ip')
    list_filter = ('issued_at', 'revoked_at')
    search_fields = ('employee__full_name', 'employee__user__username')
    readonly_fields = (
        'employee',
        'token_lookup',
        'token_digest',
        'issued_at',
        'issued_by',
        'revoked_at',
        'revoked_by',
        'last_used_at',
        'last_used_ip',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EmployeeBadgeEvent)
class EmployeeBadgeEventAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'event_type', 'employee', 'actor', 'ip_address')
    list_filter = ('event_type', 'created_at')
    search_fields = ('employee__full_name', 'actor__username', 'ip_address')
    readonly_fields = (
        'event_type',
        'employee',
        'actor',
        'ip_address',
        'details',
        'created_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
