from django.contrib import admin

from lifetracker.tasktracker.models import Task, TaskAttachment, TaskType


class ChildTaskInline(admin.TabularInline):
    model = Task
    fk_name = "parent"
    extra = 1
    fields = ("order", "title", "status", "notes")
    show_change_link = True


class TaskAttachmentInline(admin.TabularInline):
    model = TaskAttachment
    extra = 1
    fields = ("title", "date", "file_type", "file", "notes")
    autocomplete_fields = ("file_type",)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "parent", "status", "task_type", "order")
    list_filter = ("status", "task_type", "parent")
    search_fields = ("title", "notes")
    list_select_related = ("parent", "task_type")
    inlines = [ChildTaskInline, TaskAttachmentInline]
    fields = (
        "parent",
        "order",
        "title",
        "status",
        "notes",
        "task_type",
        "content_type",
        "object_id",
    )


@admin.register(TaskType)
class TaskTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "content_type")
    list_filter = ("content_type",)
    search_fields = ("name",)


@admin.register(TaskAttachment)
class TaskAttachmentAdmin(admin.ModelAdmin):
    list_display = ("title", "date", "task", "file_type", "file")
    list_filter = ("file_type",)
    search_fields = ("title", "notes", "task__title")
    autocomplete_fields = ("file_type",)
    fields = ("task", "title", "date", "notes", "file_type", "file")
