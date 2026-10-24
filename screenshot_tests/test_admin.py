"""Screenshots of admin screens, for eyeballing UI changes.

Not part of the normal test suite (they're outside `testpaths`, need the
`screenshots` dependency group and a Playwright Chromium, and aren't measured
for coverage); run them with `just screenshots`, which writes the PNGs to the
gitignored `screenshots/` directory.
"""

import datetime
import json
from pathlib import Path
from unittest import mock

import pytest
from playwright.sync_api import sync_playwright

from lifetracker.core import client_ips, uptime_log
from tests.core.mmdb_writer import write_mmdb

SCREENSHOT_DIR = Path(__file__).resolve().parent.parent / "screenshots"


# transactional_db before live_server: otherwise, if live_server is the first
# thing a test needs from the DB, pytest starts the (session-scoped) server before
# the test database exists, so it can't share the in-memory SQLite connection,
# and its request threads open connections that Django never closes.
@pytest.fixture
def page(transactional_db, live_server, settings):
    # The admin header shows this, which otherwise defaults to the machine's
    # hostname; fix it so screenshots don't vary between machines.
    settings.LIFETRACKER_BACKUP_HOSTNAME = "screenshots.example"
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            # A fixed browser time zone, other than UTC so the uptime pages'
            # local times differ from their UTC ones.
            yield browser.new_page(
                base_url=live_server.url,
                viewport={"width": 1280, "height": 800},
                timezone_id="Australia/Sydney",
            )
        finally:
            browser.close()


# Each screen is captured in both colour schemes. Switching the emulated scheme
# re-renders the current page in place, so nothing needs reloading in between.
def _screenshot(page, name):
    for color_scheme in ("light", "dark"):
        page.emulate_media(color_scheme=color_scheme)
        page.screenshot(
            path=SCREENSHOT_DIR / f"{name}-{color_scheme}.png", full_page=True
        )


def test_login(page, settings):
    # Enough Google SSO config to show its "Sign in with Google" button; the
    # credentials are fake, so the button itself wouldn't work.
    settings.GOOGLE_SSO_ENABLED = True
    settings.GOOGLE_SSO_CLIENT_ID = "screenshots-client-id"
    settings.GOOGLE_SSO_CLIENT_SECRET = "screenshots-client-secret"
    page.goto("/admin/login/")
    _screenshot(page, "login")


# admin_user before page: Django refuses ORM calls while Playwright's event loop
# is running, so the user must be created before the browser starts.
def _log_in(page, user):
    page.goto("/admin/login/")
    page.fill("input[name=username]", user.username)
    page.fill("input[name=password]", "password")
    page.click("input[type=submit]")
    page.wait_for_url("**/admin/")


def test_index(admin_user, page):
    _log_in(page, admin_user)
    _screenshot(page, "index")


def test_backups(admin_user, page):
    _log_in(page, admin_user)
    page.goto("/admin/core/cloudbackupstatus/")
    _screenshot(page, "backups")


def test_cloud_backup_list(admin_user, page, settings):
    # Fake settings and a mocked S3 client, so the page lists made-up backups
    # without touching a real bucket.
    settings.LIFETRACKER_BACKUP_S3_BUCKET_NAME = "screenshots-bucket"
    settings.LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID = "screenshots-access-key"
    settings.LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY = "screenshots-secret-key"
    start = datetime.datetime(2026, 1, 1, 9, 30, tzinfo=datetime.UTC)
    times = [start + datetime.timedelta(days=7 * week) for week in range(4)]
    objects = [
        {
            "Key": f"screenshots.example/db-backup.{t:%Y-%m-%d.%H.%M.%S}.sqlite3",
            "Size": 3_000_000 + 250_000 * i,
            "LastModified": t,
        }
        for i, t in enumerate(times)
    ]
    pages = {
        "/": [{"CommonPrefixes": [{"Prefix": "other.example/"}]}],
        "screenshots.example/": [{"Contents": objects}],
    }
    with mock.patch("lifetracker.core.backup.boto3.client") as client:
        client.return_value.get_paginator.return_value.paginate.side_effect = (
            lambda **kwargs: pages[kwargs.get("Delimiter") or kwargs["Prefix"]]
        )
        _log_in(page, admin_user)
        page.goto("/admin/core/cloudbackupstatus/list/")
        _screenshot(page, "cloud-backup-list")


def test_idle_watchdog(admin_user, page, monkeypatch, tmp_path):
    # A made-up status, as the watchdog would write it mid-SSH-session.
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)

    def iso(minutes_ago):
        return (now - datetime.timedelta(minutes=minutes_ago)).isoformat()

    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "mode": "monitor",
                "state": "active",
                "pid": 42,
                "master_pid": 41,
                "started_at": iso(95),
                "checked_at": iso(0.4),
                "check_interval_seconds": 30,
                "idle_timeout_seconds": 600,
                "last_request_at": iso(7),
                "idle_since": iso(0.4),
                "stop_due_at": iso(-9.6),
                "ports": [
                    {"port": 8000, "connections": 0, "last_connection_at": iso(7)},
                    {"port": 22, "connections": 1, "last_connection_at": iso(0.4)},
                ],
            }
        )
    )
    monkeypatch.setenv("LIFETRACKER_IDLE_STOP", "monitor")
    monkeypatch.setenv("LIFETRACKER_IDLE_STOP_STATUS_FILE", str(status_file))
    _log_in(page, admin_user)
    page.goto("/admin/idle-watchdog/")
    _screenshot(page, "idle-watchdog")


def test_uptime(admin_user, page, monkeypatch, tmp_path):
    # A made-up week: each earlier day up for a few hours in two spans (so two
    # starts), one day not at all, and today since the start of the UTC day or
    # for the last three hours, whichever is shorter.
    now = datetime.datetime.now(datetime.UTC).timestamp()
    today = uptime_log.day_of(now)
    log = uptime_log.UptimeLog(tmp_path)
    for day, hours in zip(range(today - 6, today), [2, 5, 3, 0, 9, 16], strict=True):
        if hours:
            start = day * uptime_log.SECONDS_PER_DAY
            log.record(start + 3600 * hours - 1, since=start + 3600)
            log.record(start + 3600 * (hours + 2), since=start + 3600 * (hours + 1))
    log.record(now, since=max(today * uptime_log.SECONDS_PER_DAY, now - 3 * 3600))
    monkeypatch.setenv("LIFETRACKER_UPTIME_DIR", str(tmp_path))
    _log_in(page, admin_user)
    page.goto("/admin/uptime/?days=8")
    _screenshot(page, "uptime")
    # Drill into yesterday's starts (newest first): its two up periods.
    page.get_by_role("link", name="2", exact=True).first.click()
    page.wait_for_url("**/admin/uptime/*/?days=8")
    _screenshot(page, "uptime-day")


def test_client_ips(admin_user, page, monkeypatch, tmp_path):
    # A few made-up IPs (from the documentation ranges): the owner's home and
    # phone networks, logged in, and three scanners Envoy turned away, one
    # over IPv6.
    (tmp_path / "geoip").mkdir()
    write_mmdb(
        tmp_path / "geoip" / "dbip-country-lite-2026-10.mmdb",
        {
            network: {"country": {"iso_code": code, "names": {"en": name}}}
            for network, code, name in (
                ("203.0.113.0/24", "AU", "Australia"),
                ("198.51.100.0/24", "US", "United States"),
                ("192.0.2.0/24", "NL", "Netherlands"),
                ("2001:db8::/32", "AU", "Australia"),
                ("3fff::/20", "DE", "Germany"),
            )
        },
    )
    write_mmdb(
        tmp_path / "geoip" / "dbip-asn-lite-2026-10.mmdb",
        {
            network: {
                "autonomous_system_number": asn,
                "autonomous_system_organization": org,
            }
            for network, asn, org in (
                ("203.0.113.0/24", 64500, "Example Broadband"),
                ("2001:db8::/32", 64501, "Example Mobile"),
                ("198.51.100.0/24", 64502, "Example Cloud Hosting"),
                ("192.0.2.0/24", 64503, "Example Servers B.V."),
                ("3fff::/20", 64504, "Example Hosting GmbH"),
            )
        },
    )
    now = datetime.datetime.now(datetime.UTC).timestamp()
    browser = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Firefox/140.0"
    requests = [
        *(
            client_ips.Request(
                "203.0.113.7", now - 3600 * h, "GET /admin/", 200, browser, False, True
            )
            for h in (50, 26, 2)
        ),
        client_ips.Request(
            "2001:db8:4a1c:7f00:1d3e:9b2a:c4f1:8e07",
            now - 600,
            "GET /admin/uptime/",
            200,
            browser,
            False,
            True,
        ),
        # A full-length IPv6 address, the widest an IP gets.
        client_ips.Request(
            "3fff:db8:85a3:4e21:9c7d:2b6f:a810:73e5",
            now - 1200,
            "GET /admin/config.php",
            302,
            "python-requests/2.32.3",
            True,
            False,
        ),
        *(
            client_ips.Request(
                "198.51.100.23",
                now - 60 * m,
                path,
                302,
                "Mozilla/5.0 zgrab/0.x",
                True,
                False,
            )
            for m, path in ((90, "GET /.env"), (89, "GET /wp-login.php"))
        ),
        client_ips.Request(
            "192.0.2.9", now - 300, "GET /admin/login/", 200, "curl/8.5.0", False, False
        ),
    ]
    connection = client_ips.connect(tmp_path / "client_ips.sqlite3")
    geoip = client_ips.GeoIP(tmp_path / "geoip")
    client_ips.record(connection, requests, geoip)
    geoip.close()
    connection.close()
    monkeypatch.setenv("LIFETRACKER_CLIENT_IP_DB", str(tmp_path / "client_ips.sqlite3"))
    _log_in(page, admin_user)
    page.goto("/admin/client-ips/")
    _screenshot(page, "client-ips")
