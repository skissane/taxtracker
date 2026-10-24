import datetime
import logging
import mimetypes
import sqlite3
import time
from urllib.parse import quote

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms import BaseInlineFormSet
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html_join
from django.utils.safestring import mark_safe
from django.utils.timesince import timesince

from lifetracker.core import app_icon, client_ips, idle_watchdog, uptime_log
from lifetracker.core.attachments import database_storage_file_fields
from lifetracker.core.backup import (
    CLOUD_BACKUP_ERRORS,
    CloudBackupStatusUpdateError,
    backup_database,
    backup_timestamp,
    check_sqlite,
    list_cloud_backups,
    missing_cloud_backup_settings,
    run_cloud_backup,
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


# Content types serve_file_view shows in the browser rather than downloading:
# ones a browser renders without running any of the file's script on this
# origin. Not SVG, HTML, XML or anything else a browser can treat as a page.
INLINE_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/plain",
    }
)


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

    def _can_view_stored_file(self, request, stored_file_pk):
        """Whether *request.user* can view at least one model row owning this file."""
        if request.user.is_superuser:
            return True
        path_prefix = f"db/{stored_file_pk}/"
        for model, model_admin in self.admin_site._registry.items():
            for field in database_storage_file_fields(model):
                obj = model._base_manager.filter(
                    **{f"{field.attname}__startswith": path_prefix}
                ).first()
                if obj is not None and model_admin.has_view_permission(request, obj):
                    return True
        return False

    def serve_file_view(self, request, pk):
        obj = get_object_or_404(DBStoredFile, pk=pk)
        if not self._can_view_stored_file(request, pk):
            raise PermissionDenied
        content = bytes(obj.content)
        content_type, encoding = mimetypes.guess_type(obj.filename)
        # A compressed file (e.g. "x.txt.gz" guesses as text/plain, gzip) isn't
        # the guessed type as served, so it's just bytes to download.
        if not content_type or encoding:
            content_type = "application/octet-stream"
        # Attachments can come from anywhere, and one rendered as a page on
        # this origin (HTML, SVG, XML...) could run script with the viewer's
        # admin session, so only types that can't are shown inline; anything
        # else is downloaded.
        disposition = "inline" if content_type in INLINE_CONTENT_TYPES else "attachment"
        response = HttpResponse(content, content_type=content_type)
        # Never let the browser second-guess the type into something riskier.
        response["X-Content-Type-Options"] = "nosniff"
        # And should anything still render as a document, give it a unique
        # origin with no script. Not for PDFs: browsers' built-in PDF viewers
        # refuse to run in a sandboxed document (Chrome shows "This page has
        # been blocked"), and a PDF's own JavaScript, if the viewer runs it at
        # all, can't reach the page's origin.
        if content_type != "application/pdf":
            response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        filename_encoded = quote(obj.filename, safe="")
        filename_ascii = (
            obj.filename.encode("ascii", errors="ignore")
            .decode("ascii")
            .replace("\\", "\\\\")
            .replace('"', '\\"')
        )
        cd = (
            f'{disposition}; filename="{filename_ascii}";'
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

        try:
            backup_file = backup_database()
        except CLOUD_BACKUP_ERRORS as exc:
            messages.error(request, f"DB backup failed: {exc}")
            return redirect(reverse("admin:core_cloudbackupstatus_changelist"))

        filename = f"db-backup.{backup_timestamp(timezone.now())}.sqlite3"
        # FileResponse streams the file and closes it once it's sent.
        return FileResponse(
            backup_file,
            as_attachment=True,
            filename=filename,
            content_type="application/x-sqlite3",
        )


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


def _auto_backup_status(raw):
    """The status file's automatic backup section, for the page, or None if
    automatic backups are off."""
    if raw is None:
        return None
    return {
        "running": bool(raw["running"]),
        "reason": raw["reason"],
        "last_started_at": _parse_status_time(raw["last_started_at"]),
        "last_finished_at": _parse_status_time(raw["last_finished_at"]),
        "last_exit_status": raw["last_exit_status"],
        "next_run_at": _parse_status_time(raw["next_run_at"]),
    }


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
        "auto_backup_interval": None,
    }
    try:
        config = idle_watchdog.config_from_env()
    except ValueError as exc:
        context["config_error"] = str(exc)
        return context
    context["config"] = config
    if config.auto_backup_interval_seconds is not None:
        context["auto_backup_interval"] = _format_duration(
            config.auto_backup_interval_seconds
        )
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
        idle_timeout = float(raw["idle_timeout_seconds"])
        interval = float(raw["check_interval_seconds"])
        status = {
            "mode": raw["mode"],
            "state": raw["state"],
            "pid": raw["pid"],
            "master_pid": raw["master_pid"],
            "started_at": started_at,
            "uptime": _format_duration((now - started_at).total_seconds()),
            "checked_at": checked_at,
            "idle_timeout": _format_duration(idle_timeout),
            "check_interval": _format_duration(interval),
            "idle_since": _parse_status_time(raw["idle_since"]),
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
            "auto_backup": _auto_backup_status(raw.get("auto_backup")),
        }
    except (KeyError, TypeError, ValueError) as exc:
        context["status_error"] = (
            f"Unexpected contents in {config.status_file}: {exc!r}"
        )
        return context
    # Read the request file directly rather than the status file's copy of its
    # time, which is only as fresh as the last check (issue #172). It's touched
    # after each response, so it never includes this page's own request.
    last_request = idle_watchdog.last_request_time(config.request_file)
    status["last_request_at"] = (
        datetime.datetime.fromtimestamp(last_request, datetime.UTC)
        if last_request is not None
        else None
    )
    status["checked_ago"] = (
        _format_duration((now - checked_at).total_seconds()) if checked_at else None
    )
    # Before its first check, judge by when it started instead.
    last_sign_of_life = checked_at or started_at
    status["stale"] = (
        now - last_sign_of_life
    ).total_seconds() > _IDLE_WATCHDOG_STALE_INTERVALS * interval
    status["stale_intervals"] = _IDLE_WATCHDOG_STALE_INTERVALS
    for name in ("last_request_at", "idle_since"):
        value = status[name]
        status[f"{name}_ago"] = (
            _format_duration((now - value).total_seconds()) if value else None
        )
    # Not the status file's stop_due_at, which predates this page's own
    # request: that counts as activity, so the next check restarts the timer
    # from when it finishes, about now. It only stops at a check, so up to one
    # check interval later.
    status["stop_at"] = now + datetime.timedelta(seconds=idle_timeout)
    if status["auto_backup"] and status["auto_backup"]["last_started_at"]:
        status["auto_backup"]["last_started_ago"] = _format_duration(
            (now - status["auto_backup"]["last_started_at"]).total_seconds()
        )
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


# The uptime page's default and largest ``days``.
_UPTIME_DEFAULT_DAYS = 7
_UPTIME_MAX_DAYS = 366


def _uptime_days(days_param):
    """The uptime page's ``days`` query parameter (blank for the default) as a
    number of days, or None if it's invalid."""
    if not days_param:
        return _UPTIME_DEFAULT_DAYS
    try:
        days = int(days_param)
    except ValueError:
        return None
    return days if 1 <= days <= _UPTIME_MAX_DAYS else None


def uptime_report_page_context(days_param, now=None):
    """The uptime page's context: the same report as the ``uptime_report``
    command, for the ``days`` query parameter (blank for the default)."""
    context = {
        "days": _UPTIME_DEFAULT_DAYS,
        "max_days": _UPTIME_MAX_DAYS,
        "days_error": None,
        "directory": uptime_log.uptime_dir_from_env(),
        "report": None,
    }
    days = _uptime_days(days_param)
    if days is None:
        context["days_error"] = (
            f"Days must be a whole number from 1 to {_UPTIME_MAX_DAYS}, so"
            f" showing {_UPTIME_DEFAULT_DAYS}."
        )
    else:
        context["days"] = days
    if context["directory"] is None:
        return context
    now = now if now is not None else time.time()
    # For the page's note of when today's UTC day began in the browser's time
    # zone.
    context["today_start"] = _utc_iso(uptime_log.day_date(uptime_log.day_of(now)))
    # Newest first, as on the cloud backup list.
    context["report"] = [
        {
            "row": row,
            "percent": None if row.share is None else row.share * 100,
            "up": _format_duration(row.up_seconds or 0),
            "elapsed": _format_duration(row.elapsed_seconds),
        }
        for row in reversed(
            uptime_log.daily_report(context["directory"], context["days"], now)
        )
    ]
    return context


def uptime_report_view(request):
    """The superuser-only uptime page, served and linked like the idle watchdog
    page (see ``idle_watchdog_view``)."""
    if not request.user.is_superuser:
        raise PermissionDenied

    context = {
        **admin.site.each_context(request),
        **uptime_report_page_context(request.GET.get("days", "").strip()),
        "title": "Uptime",
    }
    return render(request, "admin/core/uptime_report.html", context)


def _format_clock(second_of_day):
    """E.g. "09:05:03" for a second of a day."""
    hours, rest = divmod(second_of_day, 3600)
    return f"{hours:02}:{rest // 60:02}:{rest % 60:02}"


def _utc_iso(date, second_of_day=0):
    """E.g. "2026-10-20T09:05:03Z" for a second of a UTC day, for a ``<time>``
    element's ``datetime``, which ``core/local_times.js`` shows in the browser's
    time zone."""
    moment = datetime.datetime.combine(date, datetime.time()) + datetime.timedelta(
        seconds=second_of_day
    )
    return f"{moment.isoformat()}Z"


def uptime_day_page_context(date_param, days_param, now=None):
    """The context of the uptime page's drill-down into one UTC day
    (``date_param``, YYYY-MM-DD), listing its up periods. Raises Http404 for an
    invalid date or one after today. ``days_param`` is the Uptime page's, kept
    for the links back to it, and dropped if invalid."""
    now = now if now is not None else time.time()
    try:
        date = datetime.date.fromisoformat(date_param)
    except ValueError:
        raise Http404("Not a date.") from None
    day = (date - datetime.date(1970, 1, 1)).days
    if day > uptime_log.day_of(now):
        raise Http404("That day hasn't happened yet.")
    days = _uptime_days(days_param)
    context = {
        "date": date,
        "directory": uptime_log.uptime_dir_from_env(),
        "report_query": (f"?days={days}" if days_param and days is not None else ""),
        "previous_date": None,
        "next_date": None,
        "day": None,
        "periods": None,
        "day_start": _utc_iso(date),
        "day_end": _utc_iso(date, uptime_log.SECONDS_PER_DAY - 1),
    }
    # No day before date.min, nor any date for it to be.
    if date > datetime.date.min:
        context["previous_date"] = uptime_log.day_date(day - 1)
    if day < uptime_log.day_of(now):
        context["next_date"] = uptime_log.day_date(day + 1)
    if context["directory"] is None:
        return context
    result = uptime_log.day_periods(context["directory"], day, now)
    context["day"] = result
    if result.periods is not None:
        last = len(result.periods) - 1
        context["periods"] = [
            {
                "start": _format_clock(period.start),
                "end": _format_clock(period.end),
                "start_iso": _utc_iso(date, period.start),
                "end_iso": _utc_iso(date, period.end),
                "duration": _format_clock(period.seconds),
                "from_previous_day": index == 0 and result.from_previous_day,
                "into_next_day": index == last and result.into_next_day,
                "ongoing": index == last and result.ongoing,
            }
            for index, period in enumerate(result.periods)
        ]
        context["up"] = _format_clock(sum(p.seconds for p in result.periods))
    return context


def uptime_day_view(request, date):
    """The superuser-only drill-down from the uptime page into one day's up
    periods, served like the uptime page (see ``idle_watchdog_view``) but not
    linked from the top bar."""
    if not request.user.is_superuser:
        raise PermissionDenied

    context = {
        **admin.site.each_context(request),
        **uptime_day_page_context(date, request.GET.get("days", "").strip()),
    }
    context["title"] = f"Uptime on {context['date'].isoformat()}"
    return render(request, "admin/core/uptime_day.html", context)


# How many IPs the client IPs page lists at most.
_CLIENT_IPS_LIMIT = 500


def _utc_iso_from_timestamp(timestamp):
    """A POSIX time as e.g. "2026-10-20T09:05:03Z", or None for None."""
    if timestamp is None:
        return None
    moment = datetime.datetime.fromtimestamp(timestamp, datetime.UTC)
    return moment.replace(tzinfo=None, microsecond=0).isoformat() + "Z"


def _client_ip_row(row):
    row = dict(row)
    if "ip" in row:
        # An IPv6 address can be 39 characters: let it wrap after a colon
        # rather than widen the table.
        row["ip_html"] = format_html_join(
            mark_safe(":<wbr>"), "{}", ((part,) for part in row["ip"].split(":"))
        )
    if row.get("private_relay"):
        # Apple's rough location of the relay's user, e.g. "Sydney, AU-NSW".
        row["relay_location"] = ", ".join(
            part for part in (row.get("relay_city"), row.get("relay_region")) if part
        ) or row.get("relay_country")
    for name in ("first_seen", "last_seen", "first_logged_in", "last_logged_in"):
        if name in row:
            row[name] = _utc_iso_from_timestamp(row[name])
    return row


def _geoip_status_rows(geoip_dir):
    """The client IPs page's table of DB-IP downloads, from ``update_geoip``'s
    status file (None if there's none), and an error message or None."""
    path = geoip_dir / client_ips.GEOIP_STATUS_NAME
    try:
        statuses = client_ips.read_geoip_status(geoip_dir)
    except (OSError, ValueError) as exc:
        return None, f"Couldn't read {path}: {exc}"
    if statuses is None:
        return None, None
    rows = []
    try:
        for name in client_ips.GEOIP_SOURCES:
            if name in statuses:
                status = statuses[name]
                rows.append(
                    {
                        **status,
                        "name": name,
                        "failed": status.get("outcome") == client_ips.FAILED,
                        "attempted": _utc_iso_from_timestamp(status.get("attempted")),
                        "succeeded": _utc_iso_from_timestamp(status.get("succeeded")),
                    }
                )
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        return None, f"Unexpected contents in {path}: {exc}"
    return rows, None


def client_ips_page_context(logged_in_only=False):
    """The client IPs page's context, from the collector's database (see
    ``client_ips``)."""
    config = client_ips.config_from_env()
    context = {
        "config": config,
        "logged_in_only": logged_in_only,
        "limit": _CLIENT_IPS_LIMIT,
        "error": None,
        "ips": None,
        "networks": None,
        "geoip_files": [],
        "geoip_status": None,
        "geoip_status_error": None,
        "relay_ranges_url": client_ips.RELAY_RANGES_URL,
    }
    if config.db_path is None:
        return context
    if config.geoip_dir is not None and config.geoip_dir.is_dir():
        context["geoip_files"] = [
            path.name
            for name in client_ips.GEOIP_DATABASES
            for path in client_ips.geoip_files(config.geoip_dir, name)[:1]
        ]
    if config.geoip_dir is not None:
        context["geoip_status"], context["geoip_status_error"] = _geoip_status_rows(
            config.geoip_dir
        )
    if not config.db_path.exists():
        return context
    try:
        connection = client_ips.connect_read_only(config.db_path)
        try:
            context["ips"] = [
                _client_ip_row(row)
                for row in client_ips.recent_ips(
                    connection, logged_in_only=logged_in_only, limit=_CLIENT_IPS_LIMIT
                )
            ]
            context["total"] = client_ips.ip_count(
                connection, logged_in_only=logged_in_only
            )
            context["networks"] = [
                _client_ip_row(row) for row in client_ips.logged_in_networks(connection)
            ]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        context["error"] = f"Couldn't read {config.db_path}: {exc}"
    return context


def client_ips_view(request):
    """The superuser-only client IPs page, served and linked like the idle
    watchdog page (see ``idle_watchdog_view``)."""
    if not request.user.is_superuser:
        raise PermissionDenied

    context = {
        **admin.site.each_context(request),
        **client_ips_page_context(request.GET.get("logged_in") == "1"),
        "title": "Client IPs",
    }
    return render(request, "admin/core/client_ips.html", context)


# ---------------------------------------------------------------------------
# App icon page
# ---------------------------------------------------------------------------


class AppIconForm(forms.Form):
    image = forms.FileField(
        label="PNG image",
        help_text=(
            f"Square, at least {app_icon.MIN_SIZE}x{app_icon.MIN_SIZE} pixels "
            f"and at most {app_icon.MAX_BYTES // 1024} KiB."
        ),
        widget=forms.ClearableFileInput(attrs={"accept": "image/png"}),
    )


def app_icon_view(request):
    """The superuser-only app icon page (see ``core/app_icon.py``), served and
    linked like the idle watchdog page (see ``idle_watchdog_view``): it shows
    the current icon, and uploads a replacement or resets it to the default."""
    if not request.user.is_superuser:
        raise PermissionDenied

    form = AppIconForm()
    if request.method == "POST":
        if "reset" in request.POST:
            app_icon.reset_icon()
            messages.success(request, "The app icon is the default again.")
            return redirect("admin:app_icon_page")
        form = AppIconForm(request.POST, request.FILES)
        if form.is_valid():
            upload = form.cleaned_data["image"]
            try:
                # One byte more than allowed is enough for check_icon to refuse
                # it, without reading all of a big file into memory.
                info = app_icon.save_icon(upload.read(app_icon.MAX_BYTES + 1))
            except app_icon.InvalidIcon as exc:
                form.add_error("image", str(exc))
            else:
                messages.success(request, "The new app icon is in place.")
                if info.has_transparency:
                    messages.warning(
                        request,
                        "The image has transparency, which iOS fills in with black.",
                    )
                return redirect("admin:app_icon_page")

    context = {
        **admin.site.each_context(request),
        "title": "App Icon",
        "icon": app_icon.current_icon(),
        "form": form,
    }
    return render(request, "admin/core/app_icon.html", context)
