import io
import json

from django.core.management import call_command
from django.test import SimpleTestCase
from django.urls import reverse

from lifetracker.core.auth_whitelist import auth_whitelist, is_whitelisted


class AuthWhitelistTests(SimpleTestCase):
    """The requests that need no login, shared with the front proxy."""

    def test_matches_exact_paths_and_methods(self) -> None:
        login = reverse("admin:login")
        logout = reverse("admin:logout")
        for method, path, expected in [
            ("GET", login, True),
            ("HEAD", login, True),
            ("POST", login, True),
            ("PUT", login, False),
            ("GET", login + "x", False),
            ("GET", login.rstrip("/"), False),
            ("POST", logout, True),
            ("GET", logout, False),
            ("GET", reverse("django_google_sso:oauth_start_login"), True),
            ("GET", reverse("django_google_sso:oauth_callback"), True),
            ("POST", reverse("django_google_sso:oauth_callback"), False),
            ("GET", "/static/admin/css/login.css", True),
            ("POST", "/static/admin/css/login.css", False),
            ("GET", reverse("admin:index"), False),
            ("GET", "/healthz/", True),
            ("HEAD", "/healthz/", True),
            ("POST", "/healthz/", False),
            ("GET", "/manifest.webmanifest", True),
            ("HEAD", "/manifest.webmanifest", True),
            ("POST", "/manifest.webmanifest", False),
            ("GET", "/", False),
        ]:
            with self.subTest(method=method, path=path):
                self.assertIs(is_whitelisted(method, path), expected)

    def test_command_prints_json(self) -> None:
        out = io.StringIO()
        call_command("auth_whitelist", stdout=out)
        entries = json.loads(out.getvalue())
        self.assertIn(
            {
                "path": "/admin/login/",
                "methods": ["GET", "HEAD", "POST"],
                "prefix": False,
            },
            entries,
        )
        self.assertIn({"path": "/static/", "methods": ["GET", "HEAD"], "prefix": True}, entries)
        self.assertEqual(len(entries), len(auth_whitelist()))
