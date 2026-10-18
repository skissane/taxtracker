import datetime
import logging
import mimetypes
from urllib.parse import quote

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms import BaseInlineFormSet
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.timesince import timesince

from lifetracker.core import idle_watchdog
from lifetracker.core.backup import (
    CLOUD_BACKUP_ERRORS,
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
            except CLOUD_BACKUP_ERRORS as exc:
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
            except CLOUD_BACKUP_ERRORS as exc:
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


# ---------------------------------------------------------------------------
# Idle watchdog status page
# ---------------------------------------------------------------------------

# How many check intervals can pass without a check before the page warns that
# the watchdog may have died.
_IDLE_WATCHDOG_STALE_INTERVALS = 3


def _format_duration(seconds):
    """E.g. "28 seconds", "28 minutes" or "3 hours, 5 minutes"."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    now = timezone.now()
    return timesince(now - datetime.timedelta(seconds=seconds), now, depth=2)


def _parse_status_time(value):
    return datetime.datetime.fromisoformat(value) if value else None


def idle_watchdog_page_context(now=None):
    """The idle watchdog page's context, from the environment (the same as the
    watchdog's) and its status file."""
    now = now or timezone.now()
    context = {
        "config": None,
        "config_error": None,
        "status": None,
        "status_error": None,
        "system_uptime": None,
    }
    try:
        config = idle_watchdog.config_from_env()
    except ValueError as exc:
        context["config_error"] = str(exc)
        return context
    context["config"] = config
    uptime = idle_watchdog.system_uptime()
    if uptime is not None:
        context["system_uptime"] = _format_duration(uptime)
    try:
        raw = idle_watchdog.read_status(config.status_file)
    except (OSError, ValueError) as exc:
        context["status_error"] = f"Couldn't read {config.status_file}: {exc}"
        return context
    if raw is None:
        return context
    try:
        checked_at = _parse_status_time(raw["checked_at"])
        started_at = _parse_status_time(raw["started_at"])
        stop_due_at = _parse_status_time(raw["stop_due_at"])
        interval = float(raw["check_interval_seconds"])
        status = {
            "mode": raw["mode"],
            "state": raw["state"],
            "pid": raw["pid"],
            "master_pid": raw["master_pid"],
            "started_at": started_at,
            "uptime": _format_duration((now - started_at).total_seconds()),
            "checked_at": checked_at,
            "idle_timeout": _format_duration(raw["idle_timeout_seconds"]),
            "check_interval": _format_duration(interval),
            "last_request_at": _parse_status_time(raw.get("last_request_at")),
            "idle_since": _parse_status_time(raw["idle_since"]),
            "stop_due_at": stop_due_at,
            "ports": [
                {
                    "port": port["port"],
                    "connections": port["connections"],
                    "last_connection_at": _parse_status_time(
                        port["last_connection_at"]
                    ),
                }
                for port in raw["ports"]
            ],
        }
    except (KeyError, TypeError, ValueError) as exc:
        context["status_error"] = (
            f"Unexpected contents in {config.status_file}: {exc!r}"
        )
        return context
    status["checked_ago"] = (
        _format_duration((now - checked_at).total_seconds()) if checked_at else None
    )
    # Before its first check, judge by when it started instead.
    last_sign_of_life = checked_at or started_at
    status["stale"] = (
        now - last_sign_of_life
    ).total_seconds() > _IDLE_WATCHDOG_STALE_INTERVALS * interval
    for name in ("last_request_at", "idle_since"):
        value = status[name]
        status[f"{name}_ago"] = (
            _format_duration((now - value).total_seconds()) if value else None
        )
    remaining = (stop_due_at - now).total_seconds()
    status["stop_overdue"] = remaining <= 0
    status["stop_due_in"] = _format_duration(abs(remaining))
    for port in status["ports"]:
        seen = port["last_connection_at"]
        port["last_connection_ago"] = (
            _format_duration((now - seen).total_seconds()) if seen else None
        )
    context["status"] = status
    return context


def idle_watchdog_view(request):
    """The superuser-only idle watchdog status page. The admin site serves it
    (wrapped in ``admin_view``) at ``idle-watchdog/`` and links it from the top
    bar, as it belongs to no model."""
    if not request.user.is_superuser:
        raise PermissionDenied

    context = {
        **admin.site.each_context(request),
        **idle_watchdog_page_context(),
        "title": "Idle Watchdog",
    }
    return render(request, "admin/core/idle_watchdog.html", context)
