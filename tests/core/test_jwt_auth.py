import datetime
import io
import json
import os
import tempfile
import time
import typing
from pathlib import Path
from typing import override
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.backends.base import SessionBase
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from lifetracker.core import jwt_auth

if typing.TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse


def _session_key(session: SessionBase) -> str:
    """The session's key, which a saved session always has."""
    key = session.session_key
    assert key is not None
    return key


def _jwks_key(path: str | Path | None = None) -> jwt.PyJWK:
    """The public key from the JWKS file, as PyJWT's ``PyJWK``."""
    path = path or settings.LIFETRACKER_JWT_JWKS_FILE
    (key,) = jwt.PyJWKSet.from_json(Path(path).read_text(encoding="utf-8")).keys
    return key


def _decode_jwt(token: str) -> dict[str, typing.Any]:
    """``token``'s claims, checked as the front proxy is to check them."""
    key = _jwks_key()
    header = jwt.get_unverified_header(token)
    assert header["kid"] == key.key_id
    return jwt.decode(
        token,
        key,
        algorithms=[jwt_auth.JWT_ALGORITHM],
        audience=jwt_auth.JWT_AUDIENCE,
        issuer=jwt_auth.JWT_ISSUER,
    )


class JWTKeyTests(SimpleTestCase):
    """The JWT signing key and the JWKS written for the front proxy."""

    @override
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.dir = Path(temp_dir.name) / "config"
        self.private_path = self.dir / "jwt_private_key.pem"
        self.jwks_path = self.dir / "jwt_jwks.json"
        override = override_settings(
            LIFETRACKER_JWT_PRIVATE_KEY_FILE=self.private_path,
            LIFETRACKER_JWT_JWKS_FILE=self.jwks_path,
        )
        override.enable()
        self.addCleanup(override.disable)

    def _forget_cached_key(self) -> None:
        jwt_auth._keys.pop((self.private_path, self.jwks_path), None)

    def test_creates_private_key_and_jwks(self) -> None:
        key, kid = jwt_auth.signing_key()
        self.assertEqual(self.private_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.jwks_path.stat().st_mode & 0o777, 0o644)
        (jwk,) = json.loads(self.jwks_path.read_text(encoding="utf-8"))["keys"]
        self.assertEqual(
            {name: jwk[name] for name in ("kty", "crv", "kid", "use", "alg")},
            {"kty": "OKP", "crv": "Ed25519", "kid": kid, "use": "sig", "alg": "EdDSA"},
        )
        self.assertNotIn("d", jwk)
        # No temporary files left behind.
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["jwt_jwks.json", "jwt_private_key.pem"],
        )
        token = jwt.encode({"a": 1}, key, algorithm="EdDSA")
        self.assertEqual(
            jwt.decode(token, _jwks_key(self.jwks_path), algorithms=["EdDSA"]),
            {"a": 1},
        )

    def test_kid_is_rfc_7638_thumbprint(self) -> None:
        # RFC 8037 appendix A.3's example key and thumbprint.
        jwk = {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
        }
        self.assertEqual(
            jwt_auth._jwk_thumbprint(jwk),
            "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k",
        )

    def test_reuses_existing_key_and_rewrites_jwks(self) -> None:
        _, kid = jwt_auth.signing_key()
        pem = self.private_path.read_bytes()
        self.jwks_path.unlink()
        self._forget_cached_key()
        _, kid_again = jwt_auth.signing_key()
        self.assertEqual(kid_again, kid)
        self.assertEqual(self.private_path.read_bytes(), pem)
        self.assertEqual(_jwks_key(self.jwks_path).key_id, kid)

    def test_unchanged_jwks_not_rewritten(self) -> None:
        # So a read-only config directory works once both files exist, as with
        # `just docker-run`'s /config.
        _, kid = jwt_auth.signing_key()
        self._forget_cached_key()
        with mock.patch.object(tempfile, "mkstemp", side_effect=AssertionError("wrote")):
            self.assertEqual(jwt_auth.signing_key()[1], kid)

    def test_failed_jwks_write_leaves_no_temporary_file(self) -> None:
        self.dir.mkdir()
        with (
            mock.patch.object(os, "replace", side_effect=OSError("full")),
            self.assertRaisesRegex(OSError, "full"),
        ):
            jwt_auth._write_jwks({"kty": "OKP"}, self.jwks_path)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_key_cached_per_process(self) -> None:
        first = jwt_auth.signing_key()
        self.private_path.unlink()
        self.assertEqual(jwt_auth.signing_key(), first)
        self.assertFalse(self.private_path.exists())

    def test_create_keeps_key_another_process_wrote_first(self) -> None:
        self.dir.mkdir()
        self.private_path.write_bytes(b"theirs")
        self.assertEqual(jwt_auth._create_private_key_file(self.private_path), b"theirs")
        self.assertEqual(self.private_path.read_bytes(), b"theirs")
        self.assertEqual(list(self.dir.iterdir()), [self.private_path])

    def test_rejects_key_of_another_type(self) -> None:
        self.dir.mkdir()
        rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.private_path.write_bytes(rsa_pem)
        with self.assertRaisesRegex(ImproperlyConfigured, "isn't an Ed25519"):
            jwt_auth.signing_key()

    def test_ensure_jwt_key_command(self) -> None:
        out = io.StringIO()
        call_command("ensure_jwt_key", stdout=out)
        self.assertEqual(out.getvalue(), f"JWKS: {self.jwks_path}\n")
        self.assertTrue(self.private_path.exists())
        self.assertEqual(_jwks_key(self.jwks_path).key_id, jwt_auth.signing_key()[1])


class JWTCookieMiddlewareTests(TestCase):
    """Issuing, re-issuing, clearing and checking the JWT cookie."""

    PASSWORD = "correct horse battery staple"

    @override
    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "", self.PASSWORD)

    def _login(self) -> TestResponse:
        response = self.client.post(
            reverse("admin:login"),
            {
                "username": "admin",
                "password": self.PASSWORD,
                "next": reverse("admin:index"),
            },
        )
        self.assertRedirects(response, reverse("admin:index"))
        return response

    def _logged_in_user_id(self) -> str | None:
        return self.client.session.get("_auth_user_id")

    def _token(self) -> str:
        return self.client.cookies[jwt_auth.JWT_COOKIE_NAME].value

    def _stored(self) -> typing.Any:
        return self.client.session[jwt_auth.SESSION_KEY]

    def assertCookieDeleted(self, response: TestResponse) -> None:
        cookie = response.cookies[jwt_auth.JWT_COOKIE_NAME]
        self.assertEqual(cookie.value, "")
        self.assertEqual(cookie["max-age"], 0)

    def test_login_issues_cookie(self) -> None:
        response = self._login()
        cookie = response.cookies[jwt_auth.JWT_COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], "/")
        # DEBUG is off in tests, but SESSION_COOKIE_SECURE was set from it at
        # import, when it was on.
        self.assertEqual(bool(cookie["secure"]), settings.SESSION_COOKIE_SECURE)
        session = self.client.session
        self.assertEqual(cookie["max-age"], session.get_expiry_age())
        claims = _decode_jwt(cookie.value)
        self.assertEqual(claims["sub"], str(self.user.pk))
        self.assertAlmostEqual(claims["exp"], session.get_expiry_date().timestamp(), delta=5)
        self.assertAlmostEqual(claims["iat"], time.time(), delta=5)
        self.assertTrue(claims["jti"])
        self.assertEqual(claims["sid"], jwt_auth.session_id_claim(_session_key(session)))
        self.assertNotIn(session.session_key, cookie.value)
        self.assertEqual(self._stored(), {"sub": str(self.user.pk)})

    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_cookie_secure_like_session_cookie(self) -> None:
        response = self._login()
        self.assertTrue(response.cookies[jwt_auth.JWT_COOKIE_NAME]["secure"])

    @override_settings(SESSION_EXPIRE_AT_BROWSER_CLOSE=True)
    def test_browser_session_cookie_when_session_is(self) -> None:
        response = self._login()
        self.assertEqual(response.cookies[jwt_auth.JWT_COOKIE_NAME]["max-age"], "")

    def test_session_without_token_gets_one(self) -> None:
        # As for a session from before the middleware existed.
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            _decode_jwt(response.cookies[jwt_auth.JWT_COOKIE_NAME].value)["sub"],
            str(self.user.pk),
        )

    def test_not_reissued_while_session_unchanged(self) -> None:
        self._login()
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(jwt_auth.JWT_COOKIE_NAME, response.cookies)

    def test_reissued_when_session_saved(self) -> None:
        self._login()
        first = self._token()
        with override_settings(SESSION_SAVE_EVERY_REQUEST=True):
            response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        second = response.cookies[jwt_auth.JWT_COOKIE_NAME].value
        self.assertNotEqual(second, first)
        self.assertGreaterEqual(_decode_jwt(second)["exp"], _decode_jwt(first)["exp"])
        self.assertEqual(self._stored(), {"sub": str(self.user.pk)})

    def test_reissued_on_password_change(self) -> None:
        # update_session_auth_hash() cycles the session key, so it's saved.
        self._login()
        first = self._token()
        response = self.client.post(
            reverse("admin:password_change"),
            {
                "old_password": self.PASSWORD,
                "new_password1": "another horse battery staple",
                "new_password2": "another horse battery staple",
            },
        )
        self.assertRedirects(response, reverse("admin:password_change_done"))
        self.assertNotEqual(self._token(), first)
        self.assertEqual(
            self.client.get(reverse("admin:index")).status_code,
            200,
        )
        self.assertEqual(self._logged_in_user_id(), str(self.user.pk))

    def test_reissued_when_user_differs(self) -> None:
        self._login()
        session = self.client.session
        session[jwt_auth.SESSION_KEY] = {**self._stored(), "sub": "someone else"}
        session.save()
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(jwt_auth.JWT_COOKIE_NAME, response.cookies)
        self.assertEqual(self._stored(), {"sub": str(self.user.pk)})

    def test_concurrently_issued_tokens_all_accepted(self) -> None:
        # Two overlapping requests that both re-issue: whichever token the
        # browser keeps, the session (which records neither) accepts it.
        self._login()
        session = self.client.session
        tokens = [jwt_auth.issue_token(self.user, _session_key(session), session.get_expiry_date()) for _ in range(2)]
        self.assertNotEqual(*tokens)
        for token in tokens:
            with self.subTest(token=token):
                self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = token
                self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_earlier_token_for_session_still_accepted(self) -> None:
        self._login()
        first = self._token()
        with override_settings(SESSION_SAVE_EVERY_REQUEST=True):
            self.client.get(reverse("admin:index"))
        self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = first
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        self.assertEqual(self._logged_in_user_id(), str(self.user.pk))

    def test_logout_clears_cookie(self) -> None:
        self._login()
        response = self.client.post(reverse("admin:logout"))
        self.assertEqual(response.status_code, 200)
        self.assertCookieDeleted(response)
        self.assertIsNone(self._logged_in_user_id())

    def test_cookie_without_session_cleared(self) -> None:
        self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = "leftover"
        response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertCookieDeleted(response)

    def test_no_cookie_set_without_session(self) -> None:
        response = self.client.get(reverse("admin:login"))
        self.assertNotIn(jwt_auth.JWT_COOKIE_NAME, response.cookies)

    def test_mismatched_or_missing_cookie_logs_out(self) -> None:
        for cookie in ("tampered", "", None):
            with self.subTest(cookie=cookie):
                self._login()
                if cookie is None:
                    del self.client.cookies[jwt_auth.JWT_COOKIE_NAME]
                else:
                    self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = cookie
                with self.assertLogs("lifetracker.core.jwt_auth", "WARNING"):
                    response = self.client.get(reverse("admin:index"))
                self.assertRedirects(
                    response,
                    reverse("admin:login") + "?next=" + reverse("admin:index"),
                )
                self.assertIsNone(self._logged_in_user_id())
                if cookie is not None:
                    self.assertCookieDeleted(response)

    def _assert_rejected(self, token: str, reason: str) -> None:
        self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = token
        with self.assertLogs("lifetracker.core.jwt_auth", "WARNING") as logs:
            response = self.client.get(reverse("admin:index"))
        self.assertIn(reason, logs.output[0])
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self._logged_in_user_id())

    def test_expired_token_rejected(self) -> None:
        self._login()
        token = jwt_auth.issue_token(
            self.user,
            _session_key(self.client.session),
            timezone.now() - datetime.timedelta(seconds=1),
        )
        self._assert_rejected(token, "expired")

    def test_token_for_another_user_rejected(self) -> None:
        other = User.objects.create_superuser("other", "", self.PASSWORD)
        self._login()
        token = jwt_auth.issue_token(
            other,
            _session_key(self.client.session),
            timezone.now() + datetime.timedelta(hours=1),
        )
        self._assert_rejected(token, "another user")

    def test_token_signed_with_another_key_rejected(self) -> None:
        self._login()
        claims = _decode_jwt(self._token())
        token = jwt.encode(claims, Ed25519PrivateKey.generate(), algorithm="EdDSA")
        self._assert_rejected(token, "Signature verification failed")

    def test_token_without_sid_rejected(self) -> None:
        self._login()
        claims = _decode_jwt(self._token())
        del claims["sid"]
        token = jwt.encode(claims, jwt_auth.signing_key()[0], algorithm="EdDSA")
        self._assert_rejected(token, "sid")

    def test_new_signing_key_logs_out_once(self) -> None:
        # Nothing rotates the key; a new one only appears if the old file goes.
        self._login()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        with override_settings(
            LIFETRACKER_JWT_PRIVATE_KEY_FILE=Path(temp_dir.name) / "key.pem",
            LIFETRACKER_JWT_JWKS_FILE=Path(temp_dir.name) / "jwks.json",
        ):
            self._assert_rejected(self._token(), "Signature verification failed")
            self._login()
            self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_token_from_another_session_rejected(self) -> None:
        self._login()
        other_token = self._token()
        self.client.logout()
        self._login()
        self._assert_rejected(other_token, "another session")

    def test_whitelisted_paths_need_no_token(self) -> None:
        self.assertEqual(self.client.get(reverse("admin:login")).status_code, 200)
        response = self._login()
        self.assertIn(jwt_auth.JWT_COOKIE_NAME, response.cookies)

    def test_mismatched_cookie_logs_out_on_whitelisted_paths_too(self) -> None:
        # Otherwise the admin would send it from the login page to the index,
        # which the front proxy would send back to the login page.
        self._login()
        self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = "tampered"
        with self.assertLogs("lifetracker.core.jwt_auth", "WARNING"):
            response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self._logged_in_user_id())
        self.assertCookieDeleted(response)

    def test_not_issued_on_server_error(self) -> None:
        # SessionMiddleware doesn't save the session after a 500.
        request = RequestFactory().get("/")
        request.session = SessionStore()
        request.user = self.user
        middleware = jwt_auth.JWTCookieMiddleware(lambda r: HttpResponse(status=500))
        response = middleware(request)
        self.assertNotIn(jwt_auth.JWT_COOKIE_NAME, response.cookies)
        self.assertNotIn(jwt_auth.SESSION_KEY, request.session)
