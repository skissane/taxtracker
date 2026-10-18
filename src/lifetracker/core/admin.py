import logging
import mimetypes
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms import BaseInlineFormSet
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.timesince import timesince

from lifetracker.core.backup import (
    CloudBackupStatusUpdateError,
    backup_timestamp,
    check_sqlite,
    list_cloud_backups,
    missing_cloud_backup_settings,
    run_cloud_backup,
    serialize_database,
)
from lifetracker.core.models import (
    CLOUD_BACKUP_STATUS_PK,
    CloudBackupStatus,
    DBStoredFile,
    FileExtension,
    FileType,
    MimeType,
)

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Backups — DB download, cloud backup status and bucket listing
# ---------------------------------------------------------------------------


@admin.register(CloudBackupStatus)
class CloudBackupStatusAdmin(admin.ModelAdmin):
    """Hosts the superuser-only backup pages, in place of the usual model views.

    Its changelist URL is the backups page, so the admin index's "Backups"
    entry (under Core) leads there.
    """

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        # Only these: the single status row has no add/change/delete pages.
        return [
            path(
                "",
                self.admin_site.admin_view(self.backups_view),
                name="core_cloudbackupstatus_changelist",
            ),
            path(
                "list/",
                self.admin_site.admin_view(self.cloud_backup_list_view),
                name="core_cloudbackupstatus_list",
            ),
            path(
                "download-db-backup/",
                self.admin_site.admin_view(self.download_db_backup_view),
                name="core_cloudbackupstatus_download_db_backup",
            ),
        ]

    def backups_view(self, request):
        """Cloud backup status, with "Run Backup Now" (on POST)."""
        if not request.user.is_superuser:
            raise PermissionDenied

        status = CloudBackupStatus.objects.filter(pk=CLOUD_BACKUP_STATUS_PK).first()
        if status is None:
            status = CloudBackupStatus()
        config = {
            "endpoint_url": settings.LIFETRACKER_BACKUP_S3_ENDPOINT_URL,
            "bucket_name": settings.LIFETRACKER_BACKUP_S3_BUCKET_NAME,
            "region_name": settings.LIFETRACKER_BACKUP_S3_REGION_NAME,
            "hostname": settings.LIFETRACKER_BACKUP_HOSTNAME,
        }
        missing_required = missing_cloud_backup_settings()

        if request.method == "POST":
            try:
                object_key = run_cloud_backup()
                messages.success(
                    request,
                    f"Cloud backup uploaded successfully as {object_key}.",
                )
                return redirect(reverse("admin:core_cloudbackupstatus_changelist"))
            except CloudBackupStatusUpdateError as exc:
                # The message doesn't include the cause, so log it.
                logger.exception("Failed to record cloud backup status")
                messages.warning(
                    request,
                    "Cloud backup uploaded successfully as "
                    f"{exc.object_key}, but failed to record backup status.",
                )
            except (BotoCoreError, ClientError, RuntimeError) as exc:
                messages.error(request, f"Cloud backup failed: {exc}")

        context = {
            **self.admin_site.each_context(request),
            "title": "Backups",
            "config": config,
            "status": status,
            # One unit only ("4 days", not "4 days, 3 hours"), for a quick glance.
            "last_backup_ago": (
                timesince(status.last_backup_at, depth=1)
                if status.last_backup_at
                else None
            ),
            "missing_required": missing_required,
            "opts": self.model._meta,
        }
        return render(request, "admin/core/cloudbackupstatus/backups.html", context)

    def cloud_backup_list_view(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied

        # No "prefix" parameter means this instance's own backups; an empty one
        # means everything in the bucket.
        own_prefix = f"{settings.LIFETRACKER_BACKUP_HOSTNAME}/"
        prefix = request.GET.get("prefix", own_prefix)
        missing_required = missing_cloud_backup_settings()
        folders = []
        objects = None
        if not missing_required:
            try:
                folders, objects = list_cloud_backups(prefix)
                # Offer this instance's folder even before its first backup.
                folders = sorted({*folders, own_prefix})
            except (BotoCoreError, ClientError, RuntimeError) as exc:
                messages.error(request, f"Failed to list cloud backups: {exc}")

        context = {
            **self.admin_site.each_context(request),
            "title": "Cloud Backups",
            "bucket_name": settings.LIFETRACKER_BACKUP_S3_BUCKET_NAME,
            "prefix": prefix,
            "own_prefix": own_prefix,
            "folders": folders,
            "objects": objects,
            "total_size": sum(obj["size"] for obj in objects or []),
            "missing_required": missing_required,
            "opts": self.model._meta,
        }
        return render(
            request, "admin/core/cloudbackupstatus/cloud_backup_list.html", context
        )

    def download_db_backup_view(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied

        try:
            check_sqlite("DB backup")
        except RuntimeError as exc:
            messages.error(request, str(exc))
            return redirect(reverse("admin:core_cloudbackupstatus_changelist"))

        backup_bytes = serialize_database()
        filename = f"db-backup.{backup_timestamp(timezone.now())}.sqlite3"
        response = HttpResponse(backup_bytes, content_type="application/x-sqlite3")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
