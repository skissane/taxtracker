"""Screenshots of admin screens, for eyeballing UI changes.

Not part of the normal test suite (they're outside `testpaths`, need the
`screenshots` dependency group and a Playwright Chromium, and aren't measured
for coverage); run them with `just screenshots`, which writes the PNGs to the
gitignored `screenshots/` directory.
"""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

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
            yield browser.new_page(
                base_url=live_server.url, viewport={"width": 1280, "height": 800}
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
def test_index(admin_user, page):
    page.goto("/admin/login/")
    page.fill("input[name=username]", admin_user.username)
    page.fill("input[name=password]", "password")
    page.click("input[type=submit]")
    page.wait_for_url("**/admin/")
    _screenshot(page, "index")
