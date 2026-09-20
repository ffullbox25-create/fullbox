from django.contrib import admin

from .models import Task, TaskAttachment, TaskChecklistItem, TaskComment


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "kind",
        "assigned_to",
        "observer",
        "created_by",
        "status",
        "priority",
        "due_date",
        "created_at",
    )
    list_filter = ("kind", "status", "priority", "assigned_to", "observer")
    search_fields = ("title", "description", "route")
    ordering = ("status", "priority", "due_date", "-created_at")


@admin.register(TaskComment)
class TaskCommentAdmin(admin.ModelAdmin):
    list_display = ("task", "author", "created_at")
    search_fields = ("body",)
    ordering = ("-created_at",)


@admin.register(TaskAttachment)
class TaskAttachmentAdmin(admin.ModelAdmin):
    list_display = ("task", "uploaded_by", "uploaded_at")
    search_fields = ("file",)
    ordering = ("-uploaded_at",)


@admin.register(TaskChecklistItem)
class TaskChecklistItemAdmin(admin.ModelAdmin):
    list_display = ("task", "title", "position", "is_completed", "completed_by", "completed_at")
    list_filter = ("is_completed",)
    search_fields = ("title", "task__title")
    ordering = ("task", "position", "id")
