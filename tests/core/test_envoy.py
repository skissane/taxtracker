"""Tests for Envoy's config (core/envoy_config.py and envoy/envoy.yaml).

``EnvoyAuthTests`` runs the real Envoy, with the config the entrypoint would
write, in front of a fake upstream that echoes each request it gets. It finds
Envoy at ``$LIFETRACKER_ENVOY``, else ``.envoy/envoy`` (where `just envoy-bin`
copies the Docker image's), else on ``PATH``, and is skipped if there's none,
unless ``LIFETRACKER_REQUIRE_ENVOY`` is set, as it is in CI.
"""

import datetime
import http.client
import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from lifetracker.core import envoy_config, jwt_auth
from lifetracker.core.auth_whitelist import auth_whitelist

REPO_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "manage.py").exists()
)
TEMPLATE = (REPO_ROOT / "envoy" / "envoy.yaml").read_text(encoding="utf-8")


def _render(**kwargs):
    return envoy_config.render(
        TEMPLATE,
        **{
            "listen_host": "0.0.0.0",
            "listen_port": 8000,
            "upstream_port": 8001,
            **kwargs,
        },
    )


class EnvoyConfigRenderTests(SimpleTestCase):
    def test_fills_in_every_placeholder(self):
        config = _render()
        self.assertNotRegex(config, r"@[A-Z_]+@")
        self.assertIn('address: "0.0.0.0"\n          port_value: 8000\n', config)
        self.assertIn(
            "address: 127.0.0.1\n                      port_value: 8001\n", config
        )
        self.assertIn(f'issuer: "{jwt_auth.JWT_ISSUER}"', config)
        self.assertIn(f'audiences: ["{jwt_auth.JWT_AUDIENCE}"]', config)
        self.assertIn(f'from_cookies: ["{jwt_auth.JWT_COOKIE_NAME}"]', config)

    def test_jwks_file_comes_from_settings(self):
        with override_settings(LIFETRACKER_JWT_JWKS_FILE=Path('/keys/"odd".json')):
            config = _render()
        self.assertIn(r'filename: "/keys/\"odd\".json"', config)

    def test_rules_whitelist_each_path_and_method_then_require_the_jwt(self):
        rules = envoy_config.jwt_rules()
        expected = [
            (entry.path, entry.prefix, method)
            for entry in auth_whitelist()
            for method in entry.methods
        ]
        self.assertEqual(
            [
                (
                    rule["match"].get("path", rule["match"].get("prefix")),
                    "prefix" in rule["match"],
                    rule["match"]["headers"][0]["string_match"]["exact"],
                )
                for rule in rules[:-1]
            ],
            expected,
        )
        self.assertTrue(all("requires" not in rule for rule in rules[:-1]))
        self.assertEqual(
            rules[-1],
            {
                "match": {"prefix": "/"},
                "requires": {"provider_name": envoy_config.JWT_PROVIDER},
            },
        )
        self.assertIn(f"rules: {json.dumps(rules)}\n", _render())

    def test_login_redirect(self):
        self.assertEqual(
            envoy_config.login_redirect(),
            reverse("admin:login") + "?next=%DYNAMIC_METADATA(lifetracker:next)%",
        )

    def test_rejects_bad_settings(self):
        cases = [
            ({"listen_host": "localhost"}, "The listen host must be an IP address"),
            ({"listen_host": "0.0.0.0/"}, "The listen host must be an IP address"),
            ({"listen_port": 0}, "The listen port must be from 1 to 65535: 0"),
            ({"upstream_port": 65536}, "The upstream port must be from 1 to 65535"),
            (
                {"listen_port": 8001, "upstream_port": 8001},
                "The listen and upstream ports must differ: 8001",
            ),
        ]
        for kwargs, message in cases:
            with self.subTest(**kwargs), self.assertRaisesMessage(ValueError, message):
                _render(**kwargs)

    def test_rejects_unknown_placeholder(self):
        with self.assertRaisesMessage(ValueError, "@NOPE@"):
            envoy_config.render(
                "x: @NOPE@\n", listen_host="::", listen_port=1, upstream_port=2
            )


class EnvoyConfigCommandTests(SimpleTestCase):
    def test_writes_config_and_creates_key(self):
        with tempfile.TemporaryDirectory() as tempdir:
            temp = Path(tempdir)
            jwks = temp / "jwks.json"
            output = temp / "envoy.yaml"
            with override_settings(
                LIFETRACKER_JWT_PRIVATE_KEY_FILE=temp / "key.pem",
                LIFETRACKER_JWT_JWKS_FILE=jwks,
            ):
                call_command(
                    "envoy_config",
                    str(REPO_ROOT / "envoy" / "envoy.yaml"),
                    str(output),
                    "--listen-host=127.0.0.1",
                    "--listen-port=9000",
                    "--upstream-port=9001",
                )
                expected = _render(
                    listen_host="127.0.0.1", listen_port=9000, upstream_port=9001
                )
            self.assertEqual(output.read_text(), expected)
            self.assertTrue(jwks.exists())

    def test_reports_bad_settings(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "envoy.yaml"
            with self.assertRaisesMessage(
                CommandError, "The listen and upstream ports must differ: 9000"
            ):
                call_command(
                    "envoy_config",
                    str(REPO_ROOT / "envoy" / "envoy.yaml"),
                    str(output),
                    "--listen-host=127.0.0.1",
                    "--listen-port=9000",
                    "--upstream-port=9000",
                )
            self.assertFalse(output.exists())


def _envoy_binary():
    configured = os.environ.get("LIFETRACKER_ENVOY")
    if configured:
        return configured
    copied = REPO_ROOT / ".envoy" / "envoy"
    if copied.exists():
        return str(copied)
    return shutil.which("envoy")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    """Answers every request with a JSON description of it."""

    def _echo(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "cookie": self.headers.get("Cookie"),
                "authorization": self.headers.get("Authorization"),
                "body": self.rfile.read(length).decode(),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = _echo

    def log_message(self, format, *args):
        pass


class EnvoyAuthTests(SimpleTestCase):
    """Requests through the real Envoy, to a fake upstream."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        envoy = _envoy_binary()
        if envoy is None:
            if os.environ.get("LIFETRACKER_REQUIRE_ENVOY"):
                raise RuntimeError(
                    "Envoy not found, and LIFETRACKER_REQUIRE_ENVOY is set"
                )
            raise unittest.SkipTest("Envoy not found")

        tempdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tempdir.cleanup)
        temp = Path(tempdir.name)
        cls.key_settings = override_settings(
            LIFETRACKER_JWT_PRIVATE_KEY_FILE=temp / "jwt_private_key.pem",
            LIFETRACKER_JWT_JWKS_FILE=temp / "jwt_jwks.json",
        )

        cls.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
        cls.addClassCleanup(cls.upstream.server_close)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.upstream.shutdown)

        cls.port = _free_port()
        config_path = temp / "envoy.yaml"
        with cls.key_settings:
            jwt_auth.signing_key()
            config_path.write_text(
                envoy_config.render(
                    TEMPLATE,
                    listen_host="127.0.0.1",
                    listen_port=cls.port,
                    upstream_port=cls.upstream.server_address[1],
                )
            )
        process = subprocess.Popen(
            [
                envoy,
                "--config-path",
                str(config_path),
                "--concurrency",
                "1",
                "--disable-hot-restart",
                "--log-level",
                "warn",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        def stop():
            process.terminate()
            process.communicate(timeout=10)

        cls.addClassCleanup(stop)
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"Envoy exited: {process.stderr.read()}")
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    def _token(self, expires_in=3600, key=None, **claims):
        """A token as Django issues it, re-signed with ``key`` and ``claims``
        changed if given."""
        expires_at = timezone.now() + datetime.timedelta(seconds=expires_in)
        with self.key_settings:
            token = jwt_auth.issue_token(
                types.SimpleNamespace(pk=1), "session-key", expires_at
            )
            own_key, kid = jwt_auth.signing_key()
        if key is None and not claims:
            return token
        payload = jwt.decode(token, options={"verify_signature": False})
        return jwt.encode(
            {**payload, **claims},
            key or own_key,
            algorithm=jwt_auth.JWT_ALGORITHM,
            headers={"kid": kid},
        )

    def _request(self, method, path, *, token=None, headers=None, body=None):
        """The response's status, headers and, if it got there, the upstream's
        echo (else None)."""
        headers = dict(headers or {})
        if token is not None:
            headers["Cookie"] = f"other=1; {jwt_auth.JWT_COOKIE_NAME}={token}"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        echoed = None
        if response.headers.get("Content-Type") == "application/json":
            echoed = json.loads(data) if data else {}
        return response.status, response.headers, echoed

    def assertRedirectsToLogin(self, method, path, next_value, **kwargs):
        status, headers, echoed = self._request(method, path, **kwargs)
        self.assertEqual(status, 302, (method, path))
        self.assertEqual(
            headers["Location"], f"{reverse('admin:login')}?next={next_value}"
        )
        self.assertIsNone(echoed)

    def assertPassedThrough(self, method, path, upstream_path=None, **kwargs):
        status, _, echoed = self._request(method, path, **kwargs)
        self.assertEqual(status, 200, (method, path))
        # The upstream's JSON content type, but no body for a HEAD.
        self.assertIsNotNone(echoed)
        if method != "HEAD":
            self.assertEqual(echoed["path"], upstream_path or path)
        return echoed

    def test_no_jwt_redirects_to_login(self):
        self.assertRedirectsToLogin("GET", "/admin/", "/admin/")
        self.assertRedirectsToLogin(
            "POST",
            "/admin/taxtracker/item/add/",
            "/admin/taxtracker/item/add/",
            body="x=1",
        )
        self.assertRedirectsToLogin("GET", "/", "/")

    def test_next_is_percent_encoded_with_query_string(self):
        self.assertRedirectsToLogin(
            "GET",
            "/admin/x/?a=1&b=two%20words&c=%2F+",
            "/admin/x/%3Fa%3D1%26b%3Dtwo%2520words%26c%3D%252F%2B",
        )

    def test_valid_jwt_passes_cookie_on(self):
        token = self._token()
        echoed = self.assertPassedThrough("GET", "/admin/", token=token)
        self.assertEqual(
            echoed["cookie"], f"other=1; {jwt_auth.JWT_COOKIE_NAME}={token}"
        )
        self.assertPassedThrough(
            "POST", "/admin/taxtracker/item/add/", token=token, body="x=1"
        )

    def test_invalid_jwts_redirect_to_login(self):
        other_key = Ed25519PrivateKey.generate()
        cases = {
            "garbage": "not-a-jwt",
            "expired": self._token(expires_in=-120),
            "wrong audience": self._token(aud="lifetracker-api"),
            "wrong issuer": self._token(iss="someone-else"),
            "wrong key": self._token(key=other_key),
            "unsigned": jwt.encode(
                jwt.decode(self._token(), options={"verify_signature": False}),
                None,
                algorithm="none",
            ),
        }
        for name, token in cases.items():
            with self.subTest(name):
                self.assertRedirectsToLogin("GET", "/admin/", "/admin/", token=token)

    def test_bearer_header(self):
        self.assertPassedThrough(
            "GET", "/admin/", headers={"Authorization": f"Bearer {self._token()}"}
        )
        # Not from a browser, so a 401 rather than a redirect.
        status, headers, echoed = self._request(
            "GET", "/admin/", headers={"Authorization": "Bearer not-a-jwt"}
        )
        self.assertEqual(status, 401)
        self.assertNotIn("Location", headers)
        self.assertIsNone(echoed)

    def test_any_invalid_jwt_is_rejected(self):
        token = self._token()
        self.assertEqual(
            self._request(
                "GET", "/admin/", token=token, headers={"Authorization": "Bearer x"}
            )[0],
            401,
        )
        self.assertEqual(
            self._request(
                "GET",
                "/admin/",
                token="x",
                headers={"Authorization": f"Bearer {token}"},
            )[0],
            401,
        )

    def test_whitelist_needs_no_jwt(self):
        for entry in auth_whitelist():
            path = entry.path + ("admin/css/base.css" if entry.prefix else "")
            for method in entry.methods:
                with self.subTest(method=method, path=path):
                    self.assertPassedThrough(method, path)
                    # Nor is a bad one checked.
                    self.assertPassedThrough(method, path, token="not-a-jwt")

    def test_whitelist_ignores_query_string(self):
        self.assertPassedThrough("GET", "/admin/login/?next=/admin/")

    def test_whitelisted_paths_need_jwt_for_other_methods(self):
        self.assertRedirectsToLogin("GET", "/admin/logout/", "/admin/logout/")
        self.assertRedirectsToLogin("POST", "/healthz/", "/healthz/")
        self.assertRedirectsToLogin("DELETE", "/admin/login/", "/admin/login/")
        self.assertRedirectsToLogin("POST", "/static/x.css", "/static/x.css")

    def test_whitelist_matches_exactly(self):
        for path in (
            "/admin/login",
            "/admin/login/x",
            "/ADMIN/LOGIN/",
            "/admin/login/;x",
            "/healthz/x",
            "/static",
        ):
            with self.subTest(path):
                self.assertEqual(self._request("GET", path)[0], 302)

    def test_path_normalisation_cant_reach_another_view_via_the_whitelist(self):
        # Each normalises to a path that needs a JWT.
        for path, normalised in (
            ("/static/../admin/", "/admin/"),
            ("/static/%2e%2e/admin/", "/admin/"),
            ("/static/./../admin/", "/admin/"),
            ("/admin/login/../", "/admin/"),
            ("/healthz/../../admin/", "/admin/"),
            ("//admin/", "/admin/"),
            ("/static/..\\admin/", "/admin/"),
        ):
            with self.subTest(path):
                self.assertRedirectsToLogin("GET", path, normalised)

    def test_path_normalisation_is_what_upstream_gets(self):
        for path, normalised in (
            ("//admin//login/", "/admin/login/"),
            ("/admin/x/../login/", "/admin/login/"),
            ("/admin/logi%6E/", "/admin/login/"),
            ("/static//admin/../admin/css/base.css", "/static/admin/css/base.css"),
        ):
            with self.subTest(path):
                self.assertPassedThrough("GET", path, upstream_path=normalised)

    def test_escaped_slashes_are_rejected(self):
        for path in (
            "/static/..%2Fadmin/",
            "/static/..%2fadmin/",
            "/static/..%5Cadmin/",
        ):
            with self.subTest(path):
                status, _, echoed = self._request("GET", path)
                self.assertEqual(status, 400)
                self.assertIsNone(echoed)
