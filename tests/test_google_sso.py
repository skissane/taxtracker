import ast
import importlib.util
import typing
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django_google_sso.main import GoogleAuth

from lifetracker import google_sso
from lifetracker.core import jwt_auth

if typing.TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse
from tests.core.test_jwt_auth import _decode_jwt, _session_key

_GOOGLE_SSO_ENABLED_SETTINGS = {
    "GOOGLE_SSO_ENABLED": True,
    "GOOGLE_SSO_CLIENT_ID": "test-client-id",
    "GOOGLE_SSO_CLIENT_SECRET": "test-client-secret",
    "GOOGLE_SSO_PROJECT_ID": "test-project-id",
    "GOOGLE_SSO_CALLBACK_DOMAIN": "testserver",
}


class GoogleSSOSettingsTests(TestCase):
    """Tests for the locked-down Google SSO defaults."""

    def test_google_sso_defaults_block_new_accounts(self) -> None:
        self.assertIs(settings.GOOGLE_SSO_AUTO_CREATE_USERS, False)
        self.assertEqual(
            settings.GOOGLE_SSO_PRE_VALIDATE_CALLBACK,
            "lifetracker.google_sso.pre_validate_user",
        )

    def test_google_sso_admin_login_button_renders_when_enabled(self) -> None:
        with override_settings(**_GOOGLE_SSO_ENABLED_SETTINGS):
            response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in with Google")
        self.assertContains(response, 'href="/google_sso/login/"')
        self.assertContains(response, 'src="/static/core/google_g_logo.svg"')
        self.assertNotContains(response, "upload.wikimedia.org")

    def test_google_sso_logo_is_a_static_file(self) -> None:
        # Asserting the src attribute alone would pass even if nothing served it.
        url = settings.GOOGLE_SSO_LOGO_URL(RequestFactory().get("/"))
        path = url.removeprefix(settings.STATIC_URL)
        self.assertIsNotNone(finders.find(path))

    @override_settings(GOOGLE_SSO_ENABLED=False)
    def test_google_sso_admin_login_button_hidden_when_disabled(self) -> None:
        response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Sign in with Google")
        # The stock username/password form must still be there.
        self.assertContains(response, 'id="login-form"')

    def test_google_sso_start_login_redirects_to_google_when_enabled(self) -> None:
        with override_settings(**_GOOGLE_SSO_ENABLED_SETTINGS):
            response = self.client.get(reverse("django_google_sso:oauth_start_login"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response["Location"].startswith("https://accounts.google.com/"),
            response["Location"],
        )

    def test_google_sso_start_login_keeps_only_admin_next(self) -> None:
        # Any other next makes the library's callback fail with "Google SSO not
        # enabled for Pages"; the login page passes on next=/ for a logged-out
        # visit to the bare domain, which the proxy redirects there.
        cases = [
            ("/admin/core/", "/admin/core/"),
            ("/", "/admin/"),
            ("null", "/admin/"),
            ("https://example.com/admin/", "/admin/"),
            (None, "/admin/"),
        ]
        for next_url, expected in cases:
            with self.subTest(next_url=next_url):
                query = {} if next_url is None else {"next": next_url}
                with override_settings(**_GOOGLE_SSO_ENABLED_SETTINGS):
                    response = self.client.get(reverse("django_google_sso:oauth_start_login"), query)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.client.session["sso_next_url"], expected)

    @override_settings(GOOGLE_SSO_ENABLED=False)
    def test_google_sso_routes_404_when_disabled(self) -> None:
        for name in ("oauth_start_login", "oauth_callback"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"django_google_sso:{name}"))
                self.assertEqual(response.status_code, 404)

    def test_google_sso_urls_match_library(self) -> None:
        """lifetracker.google_sso_urls must mirror django_google_sso.urls.

        Parsed rather than imported: the library module's urlpatterns depend on
        whether SSO was enabled when it was first imported. If this fails after
        upgrading django-google-sso, update lifetracker/google_sso_urls.py.
        """

        def routes(module_name: str) -> set[tuple[object, str, object]]:
            spec = importlib.util.find_spec(module_name)
            assert spec is not None
            assert spec.origin is not None
            tree = ast.parse(Path(spec.origin).read_text())
            found: set[tuple[object, str, object]] = set()
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "path"):
                    continue
                route, view = node.args
                # Unwrap our wrappers, e.g. _require_sso_enabled(views.x), to views.x.
                while isinstance(view, ast.Call):
                    (view,) = view.args
                assert isinstance(route, ast.Constant)
                (name,) = (
                    kw.value.value for kw in node.keywords if kw.arg == "name" and isinstance(kw.value, ast.Constant)
                )
                found.add((route.value, ast.unparse(view), name))
            return found

        library_routes = routes("django_google_sso.urls")
        self.assertTrue(library_routes)
        self.assertEqual(routes("lifetracker.google_sso_urls"), library_routes)


@override_settings(LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=[])
class GoogleSSOCallbackTests(TestCase):
    """Tests for the lifetracker.google_sso callbacks django_google_sso invokes."""

    def _validate(self, email: str, verified_email: object = True) -> bool:
        return google_sso.pre_validate_user(
            {"email": email, "verified_email": verified_email}, request=RequestFactory().get("/")
        )

    def test_rejects_unverified_email(self) -> None:
        with override_settings(LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["a@x.com"]):
            for verified_email in (False, None, "true"):
                with self.subTest(verified_email=verified_email):
                    self.assertFalse(self._validate("a@x.com", verified_email))

    def test_rejects_missing_or_blank_email(self) -> None:
        with override_settings(LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["a@x.com"]):
            self.assertFalse(google_sso.pre_validate_user({}, request=RequestFactory().get("/")))
            self.assertFalse(self._validate("   "))

    def test_rejects_everyone_when_allowlist_empty(self) -> None:
        self.assertFalse(self._validate("a@example.com"))

    def test_rejects_email_not_in_allowlist(self) -> None:
        with override_settings(LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["a@x.com"]):
            self.assertFalse(self._validate("b@x.com"))

    def test_allows_allowlisted_email_case_insensitively(self) -> None:
        with override_settings(LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["x@x.com", "A@Example.com"]):
            self.assertTrue(self._validate(" a@EXAMPLE.com "))

    def test_rejects_email_shared_by_several_users(self) -> None:
        User.objects.create_user("alice", email="a@example.com")
        with override_settings(LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["a@example.com"]):
            self.assertTrue(self._validate("a@example.com"))
            User.objects.create_user("alice2", email="A@Example.com")
            self.assertFalse(self._validate("a@example.com"))


@override_settings(
    **_GOOGLE_SSO_ENABLED_SETTINGS,
    LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["a@example.com"],
)
class GoogleSSOLoginFlowTests(TestCase):
    """End-to-end tests of the library's callback view with our settings, with
    only the calls out to Google mocked."""

    def setUp(self) -> None:
        self.user = User.objects.create_user("alice", email="A@example.com", is_staff=True)

    def _callback(self, **user_info: typing.Any) -> TestResponse:
        user_info = {"email": "a@example.com", "verified_email": True} | user_info
        session = self.client.session
        session["sso_state"] = "test-state"
        session.save()
        with (
            mock.patch.object(GoogleAuth, "flow", new_callable=mock.PropertyMock),
            mock.patch.object(GoogleAuth, "get_user_info", return_value=user_info),
        ):
            return self.client.get(
                reverse("django_google_sso:oauth_callback"),
                {"code": "test-code", "state": "test-state"},
            )

    def _logged_in_user_id(self) -> str | None:
        return self.client.session.get("_auth_user_id")

    def test_allowlisted_verified_existing_user_logs_in(self) -> None:
        response = self._callback()
        self.assertRedirects(response, reverse("admin:index"))
        self.assertEqual(self._logged_in_user_id(), str(self.user.pk))

    def test_login_issues_jwt_cookie(self) -> None:
        response = self._callback()
        token = response.cookies[jwt_auth.JWT_COOKIE_NAME].value
        claims = _decode_jwt(token)
        self.assertEqual(claims["sub"], str(self.user.pk))
        session = self.client.session
        self.assertEqual(claims["sid"], jwt_auth.session_id_claim(_session_key(session)))
        self.assertEqual(session[jwt_auth.SESSION_KEY], {"sub": str(self.user.pk)})

    @override_settings(SESSION_COOKIE_AGE=12345)
    def test_session_expiry_matches_password_login(self) -> None:
        self._callback()
        session = self.client.session
        self.assertEqual(self._logged_in_user_id(), str(self.user.pk))
        # No per-session expiry override, so the global policy applies.
        self.assertNotIn("_session_expiry", session)
        self.assertEqual(session.get_expiry_age(), 12345)

    def test_rejects_email_not_in_allowlist(self) -> None:
        User.objects.create_user("bob", email="b@example.com", is_staff=True)
        self._callback(email="b@example.com")
        self.assertIsNone(self._logged_in_user_id())

    def test_rejects_unverified_email(self) -> None:
        for verified in (False, None):
            with self.subTest(verified=verified):
                self._callback(verified_email=verified)
                self.assertIsNone(self._logged_in_user_id())

    def test_does_not_create_missing_user(self) -> None:
        self.user.delete()
        self._callback()
        self.assertIsNone(self._logged_in_user_id())
        self.assertFalse(User.objects.filter(email__iexact="a@example.com").exists())

    def test_rejects_inactive_user(self) -> None:
        self.user.is_active = False
        self.user.save()
        self._callback()
        self.assertIsNone(self._logged_in_user_id())

    def test_rejects_email_shared_by_several_users(self) -> None:
        User.objects.create_user("alice2", email="a@example.com", is_staff=True)
        response = self._callback()
        self.assertRedirects(response, reverse("admin:index"), fetch_redirect_response=False)
        self.assertIsNone(self._logged_in_user_id())
