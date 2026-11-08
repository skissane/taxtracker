"""The admin pages that belong to no model: Idle Watchdog, Uptime (and its
per-day drill-down), Client IPs and App Icon.

``LifetrackerAdminSite.get_urls`` serves them, and ``base_site.html`` links them
from the top bar, as they have no admin index entry.
"""

import datetime
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.html import format_html_join
from django.utils.safestring import mark_safe
from django.utils.timesince import timesince

from lifetracker.core import app_icon, client_ips, idle_watchdog, ip_location, uptime_log

# ---------------------------------------------------------------------------
# Idle watchdog status page
# ---------------------------------------------------------------------------

# How many check intervals can pass without a check before the page warns that
# the watchdog may have died.
_IDLE_WATCHDOG_STALE_INTERVALS = 3


def _format_duration(seconds: float) -> str:
    """E.g. "28 seconds", "28 minutes" or "3 hours, 5 minutes"."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    now = timezone.now()
    return timesince(now - datetime.timedelta(seconds=seconds), now, depth=2)


def _parse_status_time(value: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(value) if value else None


def _auto_backup_status(raw: dict[str, Any] | None) -> dict[str, Any] | None:
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


def idle_watchdog_page_context(now: datetime.datetime | None = None) -> dict[str, Any]:
    """The idle watchdog page's context, from the environment (the same as the
    watchdog's) and its status file."""
    now = now or timezone.now()
    context: dict[str, Any] = {
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
        context["auto_backup_interval"] = _format_duration(config.auto_backup_interval_seconds)
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
        if started_at is None:
            raise ValueError("the status file has no start time")
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
                    "last_connection_at": _parse_status_time(port["last_connection_at"]),
                }
                for port in raw["ports"]
            ],
            "auto_backup": _auto_backup_status(raw.get("auto_backup")),
        }
    except (KeyError, TypeError, ValueError) as exc:
        context["status_error"] = f"Unexpected contents in {config.status_file}: {exc!r}"
        return context
    # Read the request file directly rather than the status file's copy of its
    # time, which is only as fresh as the last check (issue #172). It's touched
    # after each response, so it never includes this page's own request.
    last_request = idle_watchdog.last_request_time(config.request_file)
    status["last_request_at"] = (
        datetime.datetime.fromtimestamp(last_request, datetime.UTC) if last_request is not None else None
    )
    status["checked_ago"] = _format_duration((now - checked_at).total_seconds()) if checked_at else None
    # Before its first check, judge by when it started instead.
    last_sign_of_life = checked_at or started_at
    status["stale"] = (now - last_sign_of_life).total_seconds() > _IDLE_WATCHDOG_STALE_INTERVALS * interval
    status["stale_intervals"] = _IDLE_WATCHDOG_STALE_INTERVALS
    for name in ("last_request_at", "idle_since"):
        value = status[name]
        status[f"{name}_ago"] = _format_duration((now - value).total_seconds()) if value else None
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
        port["last_connection_ago"] = _format_duration((now - seen).total_seconds()) if seen else None
    context["status"] = status
    return context


def idle_watchdog_view(request: HttpRequest) -> HttpResponse:
    """The superuser-only idle watchdog status page. The admin site serves it
    (wrapped in ``admin_view``) at ``idle-watchdog/`` and links it from the top
    bar, as it belongs to no model."""
    if not request.user.is_superuser:
        raise PermissionDenied

    context: dict[str, Any] = {
        **admin.site.each_context(request),
        **idle_watchdog_page_context(),
        "title": "Idle Watchdog",
    }
    return render(request, "admin/core/idle_watchdog.html", context)


# The uptime page's default and largest ``days``.
_UPTIME_DEFAULT_DAYS = 7
_UPTIME_MAX_DAYS = 366


def _uptime_days(days_param: str | None) -> int | None:
    """The uptime page's ``days`` query parameter (blank for the default) as a
    number of days, or None if it's invalid."""
    if not days_param:
        return _UPTIME_DEFAULT_DAYS
    try:
        days = int(days_param)
    except ValueError:
        return None
    return days if 1 <= days <= _UPTIME_MAX_DAYS else None


def uptime_report_page_context(days_param: str | None, now: float | None = None) -> dict[str, Any]:
    """The uptime page's context: the same report as the ``uptime_report``
    command, for the ``days`` query parameter (blank for the default)."""
    context: dict[str, Any] = {
        "days": _UPTIME_DEFAULT_DAYS,
        "max_days": _UPTIME_MAX_DAYS,
        "days_error": None,
        "directory": uptime_log.uptime_dir_from_env(),
        "report": None,
    }
    days = _uptime_days(days_param)
    if days is None:
        context["days_error"] = (
            f"Days must be a whole number from 1 to {_UPTIME_MAX_DAYS}, so showing {_UPTIME_DEFAULT_DAYS}."
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
        for row in reversed(uptime_log.daily_report(context["directory"], context["days"], now))
    ]
    return context


def uptime_report_view(request: HttpRequest) -> HttpResponse:
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


def _format_clock(second_of_day: int) -> str:
    """E.g. "09:05:03" for a second of a day."""
    hours, rest = divmod(second_of_day, 3600)
    return f"{hours:02}:{rest // 60:02}:{rest % 60:02}"


def _utc_iso(date: datetime.date, second_of_day: int = 0) -> str:
    """E.g. "2026-10-20T09:05:03Z" for a second of a UTC day, for a ``<time>``
    element's ``datetime``, which ``core/local_times.js`` shows in the browser's
    time zone."""
    moment = datetime.datetime.combine(date, datetime.time()) + datetime.timedelta(seconds=second_of_day)
    return f"{moment.isoformat()}Z"


def uptime_day_page_context(date_param: str, days_param: str | None, now: float | None = None) -> dict[str, Any]:
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
    context: dict[str, Any] = {
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


def uptime_day_view(request: HttpRequest, date: str) -> HttpResponse:
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


def _utc_iso_from_timestamp(timestamp: float | None) -> str | None:
    """A POSIX time as e.g. "2026-10-20T09:05:03Z", or None for None."""
    if timestamp is None:
        return None
    moment = datetime.datetime.fromtimestamp(timestamp, datetime.UTC)
    return moment.replace(tzinfo=None, microsecond=0).isoformat() + "Z"


def _client_ip_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    row = dict(row)
    if "ip" in row:
        # An IPv6 address can be 39 characters: let it wrap after a colon
        # rather than widen the table.
        row["ip_html"] = format_html_join(mark_safe(":<wbr>"), "{}", ((part,) for part in row["ip"].split(":")))
    if row.get("private_relay"):
        # Apple's rough location of the relay's user, e.g. "Sydney, AU-NSW".
        row["relay_location"] = ", ".join(
            part for part in (row.get("relay_city"), row.get("relay_region")) if part
        ) or row.get("relay_country")
    for name in ("first_seen", "last_seen", "first_logged_in", "last_logged_in"):
        if name in row:
            row[name] = _utc_iso_from_timestamp(row[name])
    return row


def _geoip_status_rows(geoip_dir: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The client IPs page's table of DB-IP downloads, from ``update_geoip``'s
    status file (None if there's none), and an error message or None."""
    path = geoip_dir / ip_location.GEOIP_STATUS_NAME
    try:
        statuses = ip_location.read_geoip_status(geoip_dir)
    except (OSError, ValueError) as exc:
        return None, f"Couldn't read {path}: {exc}"
    if statuses is None:
        return None, None
    rows = []
    try:
        for name in ip_location.GEOIP_SOURCES:
            if name in statuses:
                status = statuses[name]
                rows.append(
                    {
                        **status,
                        "name": name,
                        "failed": status.get("outcome") == ip_location.FAILED,
                        "attempted": _utc_iso_from_timestamp(status.get("attempted")),
                        "succeeded": _utc_iso_from_timestamp(status.get("succeeded")),
                    }
                )
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        return None, f"Unexpected contents in {path}: {exc}"
    return rows, None


def client_ips_page_context(logged_in_only: bool = False) -> dict[str, Any]:
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
        "relay_ranges_url": ip_location.RELAY_RANGES_URL,
    }
    if config.db_path is None:
        return context
    if config.geoip_dir is not None and config.geoip_dir.is_dir():
        context["geoip_files"] = [
            path.name
            for name in ip_location.GEOIP_DATABASES
            for path in ip_location.geoip_files(config.geoip_dir, name)[:1]
        ]
    if config.geoip_dir is not None:
        context["geoip_status"], context["geoip_status_error"] = _geoip_status_rows(config.geoip_dir)
    if not config.db_path.exists():
        return context
    try:
        connection = client_ips.connect_read_only(config.db_path)
        try:
            context["ips"] = [
                _client_ip_row(row)
                for row in client_ips.recent_ips(connection, logged_in_only=logged_in_only, limit=_CLIENT_IPS_LIMIT)
            ]
            context["total"] = client_ips.ip_count(connection, logged_in_only=logged_in_only)
            context["networks"] = [_client_ip_row(row) for row in client_ips.logged_in_networks(connection)]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        context["error"] = f"Couldn't read {config.db_path}: {exc}"
    return context


def client_ips_view(request: HttpRequest) -> HttpResponse:
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


def app_icon_view(request: HttpRequest) -> HttpResponse:
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
