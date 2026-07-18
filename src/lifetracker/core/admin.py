import mimetypes
from urllib.parse import quote

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import path

from lifetracker.core.models import DBStoredFile, FileExtension, FileType, MimeType

# ---------------------------------------------------------------------------
# Formsets — "at least one primary" validation
# ---------------------------------------------------------------------------


class _AtLeastOnePrimaryFormSet(BaseInlineFormSet):
    """Base formset that enforces exactly one row is marked is_primary=True.

    Rules:
    - If _nonempty_error is set and there are no non-deleted rows, raise it.
    - If exactly one non-deleted row exists and none is marked primary,
      automatically mark it as primary (and persist the change).
    - If multiple rows exist and none is marked primary, raise ValidationError.
    - If multiple rows are marked primary, raise ValidationError (prevents
      IntegrityError from the unique DB constraint).
    """

    _primary_label = "item"
    _nonempty_error = None  # if set, raised when there are no non-deleted rows
    _auto_primary_form = None  # set in clean() when auto-setting a single row

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        non_deleted_forms = [
            f
            for f in self.forms
            if f.cleaned_data and not f.cleaned_data.get("DELETE", False)
        ]
        if not non_deleted_forms:
            if self._nonempty_error:
                raise ValidationError(self._nonempty_error)
            return
        primary_forms = [
            f for f in non_deleted_forms if f.cleaned_data.get("is_primary")
        ]
        if len(primary_forms) > 1:
            raise ValidationError(
                f"Only one {self._primary_label} may be marked as primary."
            )
        if not primary_forms:
            if len(non_deleted_forms) == 1:
                # Exactly one row — auto-set it as primary.
                # We update both cleaned_data (used if the form itself is saved
                # via form.save()) and instance.is_primary (used as a fallback for
                # unchanged existing rows that form.save() may skip).
                non_deleted_forms[0].cleaned_data["is_primary"] = True
                non_deleted_forms[0].instance.is_primary = True
                self._auto_primary_form = non_deleted_forms[0]
            else:
                raise ValidationError(
                    f"At least one {self._primary_label} must be marked as primary."
                )

    def save(self, commit=True):
        instances = super().save(commit=commit)
        # For the auto-set-primary case, ensure the DB is updated even when the
        # form was not otherwise "changed" (e.g. an existing row already in the DB
        # whose is_primary was False and the user didn't explicitly tick the box).
        if commit and self._auto_primary_form is not None:
            inst = self._auto_primary_form.instance
            # clean() sets inst.is_primary = True in memory, so that value cannot
            # be used to decide whether persistence is needed. For existing rows,
            # unconditionally issue the UPDATE so unchanged forms are persisted.
            # New rows are already saved by super().save() with is_primary=True.
            if inst.pk:
                self.model.objects.filter(pk=inst.pk).update(is_primary=True)
            inst.is_primary = True
        return instances


class MimeTypeFormSet(_AtLeastOnePrimaryFormSet):
    _primary_label = "MIME type"
    _nonempty_error = "A file type must have at least one MIME type."


class FileExtensionFormSet(_AtLeastOnePrimaryFormSet):
    _primary_label = "file extension"
    _nonempty_error = "A file type must have at least one file extension."


# ---------------------------------------------------------------------------
# FileType Admin
# ---------------------------------------------------------------------------


class MimeTypeInline(admin.TabularInline):
    model = MimeType
    extra = 1
    fields = ("mime_type", "is_primary")
    formset = MimeTypeFormSet


class FileExtensionInline(admin.TabularInline):
    model = FileExtension
    extra = 1
    fields = ("extension", "is_primary")
    formset = FileExtensionFormSet


@admin.register(FileType)
class FileTypeAdmin(admin.ModelAdmin):
    list_display = ("short_name", "full_name", "primary_mime_type", "primary_extension")
    search_fields = ("short_name", "full_name")
    inlines = [MimeTypeInline, FileExtensionInline]

    @admin.display(description="Primary MIME Type")
    def primary_mime_type(self, obj):
        mt = obj.mime_types.filter(is_primary=True).first()
        return mt.mime_type if mt else "—"

    @admin.display(description="Primary Extension")
    def primary_extension(self, obj):
        ext = obj.file_extensions.filter(is_primary=True).first()
        return ext.extension if ext else "—"


@admin.register(MimeType)
class MimeTypeAdmin(admin.ModelAdmin):
    list_display = ("mime_type", "file_type", "is_primary")
    list_filter = ("file_type", "is_primary")
    search_fields = ("mime_type",)
    list_select_related = ("file_type",)


@admin.register(FileExtension)
class FileExtensionAdmin(admin.ModelAdmin):
    list_display = ("extension", "file_type", "is_primary")
    list_filter = ("file_type", "is_primary")
    search_fields = ("extension",)
    list_select_related = ("file_type",)


# ---------------------------------------------------------------------------
# DBStoredFile Admin — not user-facing, hosts the file-serving view only
# ---------------------------------------------------------------------------


@admin.register(DBStoredFile)
class DBStoredFileAdmin(admin.ModelAdmin):
    """Registered only to host the serve-file URL; hidden from the admin index."""

    def has_module_permission(self, request):
        return False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        custom = [
            path(
                "file/<int:pk>/",
                self.admin_site.admin_view(self.serve_file_view),
                name="core_dbstoredfile_serve_file",
            ),
        ]
        return custom + super().get_urls()

    def serve_file_view(self, request, pk):
        obj = get_object_or_404(DBStoredFile, pk=pk)
        content = bytes(obj.content)
        content_type, _ = mimetypes.guess_type(obj.filename)
        if not content_type:
            content_type = "application/octet-stream"
        response = HttpResponse(content, content_type=content_type)
        filename_encoded = quote(obj.filename, safe="")
        filename_ascii = (
            obj.filename.encode("ascii", errors="ignore")
            .decode("ascii")
            .replace("\\", "\\\\")
            .replace('"', '\\"')
        )
        cd = (
            f'inline; filename="{filename_ascii}";'
            f" filename*=UTF-8''{filename_encoded}"
        )
        response["Content-Disposition"] = cd
        return response
