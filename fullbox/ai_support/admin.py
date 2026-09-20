from django.contrib import admin

from .models import ActionApproval, AgentAction, Incident, IncidentMessage


class IncidentMessageInline(admin.TabularInline):
    model = IncidentMessage
    extra = 0
    readonly_fields = ("created_at",)


class AgentActionInline(admin.TabularInline):
    model = AgentAction
    extra = 0
    readonly_fields = ("policy_level", "created_at", "updated_at")


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ("number", "zone", "object_id", "reporter", "severity", "status", "updated_at")
    list_filter = ("status", "severity", "zone", "inventory_changed", "movements_changed")
    search_fields = ("object_id", "description", "reporter__username")
    readonly_fields = ("created_at", "updated_at", "closed_at")
    inlines = (IncidentMessageInline, AgentActionInline)


@admin.register(IncidentMessage)
class IncidentMessageAdmin(admin.ModelAdmin):
    list_display = ("incident", "sender_type", "author", "created_at")
    list_filter = ("sender_type",)
    search_fields = ("body", "incident__object_id")


@admin.register(AgentAction)
class AgentActionAdmin(admin.ModelAdmin):
    list_display = ("incident", "action_type", "policy_level", "status", "approved_by", "updated_at")
    list_filter = ("policy_level", "status", "action_type")
    readonly_fields = ("policy_level", "created_at", "updated_at")


@admin.register(ActionApproval)
class ActionApprovalAdmin(admin.ModelAdmin):
    list_display = ("action", "decision", "requested_by", "decided_by", "created_at")
    list_filter = ("decision",)
