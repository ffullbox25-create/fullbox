from django.contrib import admin

from .models import (
    EmployeeCoverage,
    TaskHandoff,
    TeamReportExportJob,
    TeamReportFavorite,
    TeamSavedReport,
)


@admin.register(TaskHandoff)
class TaskHandoffAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "action", "from_employee", "to_employee", "author", "created_at")
    list_filter = ("action", "created_at")
    search_fields = ("task__title", "reason", "comment")


@admin.register(EmployeeCoverage)
class EmployeeCoverageAdmin(admin.ModelAdmin):
    list_display = ("id", "principal", "substitute", "valid_from", "valid_to", "is_active")
    list_filter = ("is_active", "valid_from")
    search_fields = ("principal__full_name", "substitute__full_name", "note")


@admin.register(TeamReportFavorite)
class TeamReportFavoriteAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "section", "report_code", "last_generated_at", "updated_at")
    list_filter = ("section", "updated_at")
    search_fields = ("user__username", "section", "report_code")


@admin.register(TeamSavedReport)
class TeamSavedReportAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "user", "section", "report_code", "export_format", "updated_at")
    list_filter = ("section", "export_format", "updated_at")
    search_fields = ("title", "user__username", "section", "report_code")


@admin.register(TeamReportExportJob)
class TeamReportExportJobAdmin(admin.ModelAdmin):
    list_display = ("id", "report_title", "user", "section", "report_code", "export_format", "status", "row_count", "created_at")
    list_filter = ("status", "section", "export_format", "created_at")
    search_fields = ("report_title", "user__username", "section", "report_code", "error_text")
