import ast
import io
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import typing
from pathlib import Path
from unittest import mock

import coverage
from django.conf import settings
from django.contrib.auth.models import User
from django.db.utils import ConnectionHandler
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.log import ServerFormatter

from lifetracker.core.backup import backup_database
from lifetracker.settings import fly_machine_url


class _SettingsSubprocessMixin:
    """settings.py computes its values at import time, from files under $HOME
    and from environment variables, before Django is even configured. They can
    only be exercised by importing it fresh in a subprocess with a controlled
    $HOME and environment — the version already imported for this test run
    reflects whatever the real ~/.config and environment looked like when the
    suite started."""

    REPO_ROOT = Path(__file__).resolve().parent.parent
    SRC_DIR = REPO_ROOT / "src"

    def _run(
        self, home: str | Path, extra_env: dict[str, str] | None = None, expr: str = "s.SECRET_KEY"
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(self.SRC_DIR),
        }
        if extra_env:
            env.update(extra_env)

        code = f"import lifetracker.settings as s; print({expr})"
        # When this test run itself is under `coverage run`, also measure the
        # child interpreter, so the many settings.py branches only reachable
        # via subprocess (see class docstring) count towards the total.
        if coverage.Coverage.current() is not None:
            env["COVERAGE_PROCESS_START"] = str(self.REPO_ROOT / "pyproject.toml")
            code = "import coverage; coverage.process_startup(); " + code

        return subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            cwd=str(self.REPO_ROOT),
            capture_output=True,
            text=True,
        )


class SettingsSecretKeyTests(_SettingsSubprocessMixin, TestCase):
    """SECRET_KEY is read from (or generated into) a file under $HOME."""

    def test_fresh_install_generates_and_persists_a_key(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            printed_key = proc.stdout.strip()
            self.assertTrue(printed_key)

            key_file = Path(home) / ".config" / "lifetracker" / "secret_key"
            self.assertEqual(key_file.read_text().strip(), printed_key)
            self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)

    def test_old_taxtracker_key_is_copied_forward(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            (old_dir / "secret_key").write_text("legacy-secret-key-content\n")

            proc = self._run(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "legacy-secret-key-content")

            new_key_file = Path(home) / ".config" / "lifetracker" / "secret_key"
            self.assertEqual(new_key_file.read_text().strip(), "legacy-secret-key-content")

    def test_fresh_install_does_not_require_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as patch_dir:
            (Path(patch_dir) / "sitecustomize.py").write_text(
                "from pathlib import Path\n"
                "def _deny_chmod(self, mode):\n"
                "    raise PermissionError('blocked chmod')\n"
                "Path.chmod = _deny_chmod\n"
            )
            proc = self._run(
                home,
                extra_env={"PYTHONPATH": os.pathsep.join((patch_dir, str(self.SRC_DIR)))},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(proc.stdout.strip())

    def test_unreadable_old_key_falls_back_to_fresh_key(self) -> None:
        """An OSError while reading the legacy key must not be fatal — it
        should just skip the migration and generate a fresh key instead."""
        with tempfile.TemporaryDirectory() as home:
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            old_file = old_dir / "secret_key"
            old_file.write_text("legacy-secret-key-content\n")
            old_file.chmod(0o000)
            try:
                proc = self._run(home)
            finally:
                old_file.chmod(0o600)  # so TemporaryDirectory cleanup can remove it

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotEqual(proc.stdout.strip(), "legacy-secret-key-content")

    def test_migration_write_failure_is_swallowed_and_falls_through(self) -> None:
        """If the new key's directory can't be created during migration, that
        failure is swallowed — but the identical failure then hits again
        during fresh-key generation, which is fatal."""
        with tempfile.TemporaryDirectory() as home:
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            (old_dir / "secret_key").write_text("legacy-secret-key-content\n")
            # A file where the new key's parent directory should be.
            (Path(home) / ".config" / "lifetracker").write_text("not a directory")

            proc = self._run(home)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Could not write secret key file", proc.stderr)

    def test_unreadable_existing_key_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            new_dir = Path(home) / ".config" / "lifetracker"
            new_dir.mkdir(parents=True)
            new_file = new_dir / "secret_key"
            new_file.write_text("blocked\n")
            new_file.chmod(0o000)
            try:
                proc = self._run(home)
            finally:
                new_file.chmod(0o600)

            self.assertEqual(proc.returncode, 1)
            self.assertIn("Could not read secret key file", proc.stderr)

    def test_unwritable_key_directory_raises(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / ".config"
            config_dir.mkdir(parents=True)
            # A file where the new key's parent directory should be, and no
            # legacy key present — so migration is skipped entirely and the
            # fresh-key write is the first (and only) thing that fails.
            (config_dir / "lifetracker").write_text("not a directory")

            proc = self._run(home)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Could not write secret key file", proc.stderr)

    def test_override_env_var_skips_migration(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            (old_dir / "secret_key").write_text("legacy-secret-key-content\n")
            override_path = Path(home) / "custom" / "secret_key"

            proc = self._run(home, extra_env={"LIFETRACKER_SECRET_KEY_FILE": str(override_path)})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotEqual(proc.stdout.strip(), "legacy-secret-key-content")
            self.assertTrue(override_path.exists())


class SettingsDataDirTests(_SettingsSubprocessMixin, TestCase):
    """LIFETRACKER_DATA_DIR supplies default locations for the database and
    config files, LIFETRACKER_CONFIG_DIR moves the config files elsewhere, and
    LIFETRACKER_DB_PATH and LIFETRACKER_SECRET_KEY_FILE override each file."""

    EXPR = "(str(s.DATABASES['default']['NAME']), str(s._key_file))"

    def test_paths_default_to_the_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            # A legacy key under $HOME must not be copied into the data dir.
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            (old_dir / "secret_key").write_text("legacy-secret-key-content\n")
            data_dir = Path(home) / "data"
            data_dir.mkdir()

            proc = self._run(
                home,
                extra_env={"LIFETRACKER_DATA_DIR": str(data_dir)},
                expr=f"{self.EXPR} + (s.SECRET_KEY, str(s.LIFETRACKER_CONFIG_DIR))",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            db_path, key_path, key, config_dir = ast.literal_eval(proc.stdout.strip())
            self.assertEqual(config_dir, str(data_dir))
            self.assertEqual(db_path, str(data_dir / "db.sqlite3"))
            self.assertEqual(key_path, str(data_dir / "secret_key"))
            self.assertNotEqual(key, "legacy-secret-key-content")
            self.assertEqual((data_dir / "secret_key").read_text().strip(), key)
            self.assertFalse((Path(home) / ".config" / "lifetracker").exists())

    def test_explicit_paths_take_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            db_path = Path(home) / "elsewhere" / "db.sqlite3"
            key_path = Path(home) / "elsewhere" / "secret_key"

            proc = self._run(
                home,
                extra_env={
                    "LIFETRACKER_DATA_DIR": str(data_dir),
                    "LIFETRACKER_DB_PATH": str(db_path),
                    "LIFETRACKER_SECRET_KEY_FILE": str(key_path),
                },
                expr=self.EXPR,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(ast.literal_eval(proc.stdout.strip()), (str(db_path), str(key_path)))
            self.assertFalse(data_dir.exists())

    def test_config_dir_takes_precedence_over_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            # A legacy key under $HOME must not be copied into the config dir.
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            (old_dir / "secret_key").write_text("legacy-secret-key-content\n")
            data_dir = Path(home) / "data"
            config_dir = Path(home) / "config"

            proc = self._run(
                home,
                extra_env={
                    "LIFETRACKER_DATA_DIR": str(data_dir),
                    "LIFETRACKER_CONFIG_DIR": str(config_dir),
                },
                expr=f"{self.EXPR} + (s.SECRET_KEY, str(s.LIFETRACKER_CONFIG_DIR))",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            db_path, key_path, key, resolved_config_dir = ast.literal_eval(proc.stdout.strip())
            self.assertEqual(resolved_config_dir, str(config_dir))
            self.assertEqual(key_path, str(config_dir / "secret_key"))
            self.assertEqual(db_path, str(data_dir / "db.sqlite3"))
            self.assertNotEqual(key, "legacy-secret-key-content")
            self.assertFalse(data_dir.exists())

    def test_without_data_dir_db_defaults_to_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, expr=f"{self.EXPR} + (str(s.LIFETRACKER_CONFIG_DIR),)")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            db_path, _, config_dir = ast.literal_eval(proc.stdout.strip())
            self.assertEqual(db_path, str(self.REPO_ROOT / "db.sqlite3"))
            self.assertEqual(config_dir, str(Path(home) / ".config" / "lifetracker"))


class SettingsEnvTests(_SettingsSubprocessMixin, TestCase):
    """DEBUG, ALLOWED_HOSTS and the HTTPS-related settings come from
    LIFETRACKER_* environment variables."""

    EXPR = (
        "(s.DEBUG, s.ALLOWED_HOSTS, s.CSRF_TRUSTED_ORIGINS,"
        " getattr(s, 'SECURE_PROXY_SSL_HEADER', None),"
        " s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, s.SECURE_HSTS_SECONDS > 0)"
    )

    def _settings(self, **env: str) -> typing.Any:
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env=env, expr=self.EXPR)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return ast.literal_eval(proc.stdout.strip())

    def _error(self, **env: str) -> str:
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env=env, expr=self.EXPR)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ImproperlyConfigured", proc.stderr)
        return proc.stderr

    def test_defaults_are_local_development(self) -> None:
        self.assertEqual(self._settings(), (True, [], [], None, False, False, False))

    def test_debug_off_with_hosts(self) -> None:
        self.assertEqual(
            self._settings(
                LIFETRACKER_DEBUG="0",
                LIFETRACKER_ALLOWED_HOSTS=" tax.example.com, other.example.com ,",
            ),
            (
                False,
                ["tax.example.com", "other.example.com"],
                [],
                None,
                True,
                True,
                True,
            ),
        )

    def test_debug_off_without_hosts_refuses_to_start(self) -> None:
        stderr = self._error(LIFETRACKER_DEBUG="false")
        self.assertIn("LIFETRACKER_ALLOWED_HOSTS must be set", stderr)

    def test_invalid_bool_is_rejected(self) -> None:
        stderr = self._error(LIFETRACKER_DEBUG="yes")
        self.assertIn("LIFETRACKER_DEBUG must be 1/true or 0/false", stderr)

    def test_bool_is_case_insensitive(self) -> None:
        self.assertTrue(self._settings(LIFETRACKER_DEBUG="TRUE")[0])
        self.assertFalse(self._settings(LIFETRACKER_DEBUG="False", LIFETRACKER_ALLOWED_HOSTS="example.com")[0])

    def test_hosts_default_from_fly_app_name(self) -> None:
        self.assertEqual(
            self._settings(LIFETRACKER_DEBUG="0", FLY_APP_NAME="example-app")[1],
            ["example-app.fly.dev"],
        )

    def test_explicit_hosts_override_fly_app_name(self) -> None:
        self.assertEqual(
            self._settings(
                LIFETRACKER_DEBUG="0",
                LIFETRACKER_ALLOWED_HOSTS="tax.example.com",
                FLY_APP_NAME="example-app",
            )[1],
            ["tax.example.com"],
        )

    def test_proxy_settings(self) -> None:
        debug, _, origins, proxy_header, _, _, hsts_on = self._settings(
            LIFETRACKER_DEBUG="true",
            LIFETRACKER_CSRF_TRUSTED_ORIGINS="https://tax.example.com",
            LIFETRACKER_TRUST_X_FORWARDED_PROTO="1",
        )
        self.assertTrue(debug)
        self.assertEqual(origins, ["https://tax.example.com"])
        self.assertEqual(proxy_header, ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertFalse(hsts_on)


class SettingsAdminHeaderEnvTests(_SettingsSubprocessMixin, TestCase):
    EXPR = (
        "(s.LIFETRACKER_COMMIT_URL_BASE, s.LIFETRACKER_COMMIT_ID,"
        " s.LIFETRACKER_BACKUP_HOSTNAME, s.LIFETRACKER_UNAUTHENTICATED_SITE_NAME)"
    )

    def _settings(self, **env: str) -> typing.Any:
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env=env, expr=self.EXPR)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return ast.literal_eval(proc.stdout.strip())

    def test_defaults(self) -> None:
        commit_url_base, commit_id, backup_hostname, unauthenticated_name = self._settings()
        self.assertEqual(commit_url_base, "https://github.com/skissane/taxtracker/commit/")
        self.assertEqual(commit_id, "")
        self.assertTrue(backup_hostname)
        self.assertEqual(unauthenticated_name, "Life Tracker")

    def test_reads_commit_values_from_environment(self) -> None:
        self.assertEqual(
            self._settings(
                LIFETRACKER_COMMIT_URL_BASE="https://example.invalid/commit/",
                LIFETRACKER_COMMIT_ID="deadbeef",
                LIFETRACKER_BACKUP_HOSTNAME="test-instance",
                LIFETRACKER_UNAUTHENTICATED_SITE_NAME=" Example Site ",
            ),
            (
                "https://example.invalid/commit/",
                "deadbeef",
                "test-instance",
                "Example Site",
            ),
        )

    def test_blank_unauthenticated_site_name_uses_default(self) -> None:
        self.assertEqual(
            self._settings(LIFETRACKER_UNAUTHENTICATED_SITE_NAME="  ")[3],
            "Life Tracker",
        )

    def test_backup_hostname_defaults_from_fly_app_name(self) -> None:
        self.assertEqual(self._settings(FLY_APP_NAME="example-app")[2], "example-app.fly.dev")

    def test_explicit_backup_hostname_overrides_fly_app_name(self) -> None:
        self.assertEqual(
            self._settings(LIFETRACKER_BACKUP_HOSTNAME="test-instance", FLY_APP_NAME="example-app")[2],
            "test-instance",
        )


class FlyMachineUrlTests(SimpleTestCase):
    def test_built_from_app_name_and_machine_id(self) -> None:
        self.assertEqual(
            fly_machine_url(" example-app ", "0123456789abcd "),
            "https://fly.io/apps/example-app/machines/0123456789abcd",
        )

    def test_values_are_url_quoted(self) -> None:
        self.assertEqual(fly_machine_url("a/b", "c?d"), "https://fly.io/apps/a%2Fb/machines/c%3Fd")

    def test_empty_unless_both_are_set(self) -> None:
        for app_name, machine_id in [
            ("", ""),
            ("example-app", ""),
            ("", "0123456789abcd"),
            ("example-app", "  "),
            (" ", "0123456789abcd"),
        ]:
            with self.subTest(app_name=app_name, machine_id=machine_id):
                self.assertEqual(fly_machine_url(app_name, machine_id), "")


class SettingsFlyMachineUrlTests(_SettingsSubprocessMixin, TestCase):
    def _url(self, **env: str) -> typing.Any:
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env=env, expr="repr(s.LIFETRACKER_FLY_MACHINE_URL)")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return ast.literal_eval(proc.stdout.strip())

    def test_read_from_fly_environment_variables(self) -> None:
        self.assertEqual(
            self._url(FLY_APP_NAME="example-app", FLY_MACHINE_ID="0123456789abcd"),
            "https://fly.io/apps/example-app/machines/0123456789abcd",
        )


class SettingsGoogleSSOEnvTests(_SettingsSubprocessMixin, TestCase):
    """Google SSO is configured only from LIFETRACKER_GOOGLE_SSO_* variables."""

    EXPR = (
        "(s.GOOGLE_SSO_ENABLED, s.GOOGLE_SSO_CLIENT_ID, s.GOOGLE_SSO_CLIENT_SECRET,"
        " s.GOOGLE_SSO_PROJECT_ID, s.GOOGLE_SSO_CALLBACK_DOMAIN,"
        " s.LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS)"
    )

    def _settings(self, **env: str) -> typing.Any:
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env=env, expr=self.EXPR)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return ast.literal_eval(proc.stdout.strip())

    def test_defaults_disabled(self) -> None:
        self.assertEqual(self._settings(), (False, "", "", "", None, []))

    def test_reads_lifetracker_variables(self) -> None:
        self.assertEqual(
            self._settings(
                LIFETRACKER_GOOGLE_SSO_CLIENT_ID=" id ",
                LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET=" secret ",
                LIFETRACKER_GOOGLE_SSO_PROJECT_ID=" project ",
                LIFETRACKER_GOOGLE_SSO_CALLBACK_DOMAIN=" tax.example.com ",
                LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=" a@x.com, ,b@x.com,",
            ),
            (
                True,
                "id",
                "secret",
                "project",
                "tax.example.com",
                ["a@x.com", "b@x.com"],
            ),
        )

    def test_enabled_needs_both_id_and_secret(self) -> None:
        for env in (
            {"LIFETRACKER_GOOGLE_SSO_CLIENT_ID": "id"},
            {"LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET": "secret"},
        ):
            with self.subTest(env=env):
                self.assertFalse(self._settings(**env)[0])

    def test_ignores_unprefixed_variables(self) -> None:
        self.assertEqual(
            self._settings(
                GOOGLE_SSO_CLIENT_ID="id",
                GOOGLE_SSO_CLIENT_SECRET="secret",
                GOOGLE_SSO_PROJECT_ID="project",
                GOOGLE_SSO_CALLBACK_DOMAIN="tax.example.com",
            ),
            (False, "", "", "", None, []),
        )


class LoggingTests(TestCase):
    """settings.LOGGING sends Django's and the app's logs to stderr whatever
    DEBUG is (tests run with it off, as production does), so they reach the
    Fly logs."""

    def setUp(self) -> None:
        # Both loggers share the one handler, which writes to stderr.
        (handler,) = logging.getLogger("django").handlers
        self.assertEqual(logging.getLogger("lifetracker").handlers, [handler])
        assert type(handler) is logging.StreamHandler
        self.stream = io.StringIO()
        self.addCleanup(handler.setStream, handler.setStream(self.stream))

    def test_unhandled_exception_traceback_is_logged(self) -> None:
        client = Client(raise_request_exception=False)
        client.force_login(User.objects.create_superuser("admin", "", "pw"))
        with mock.patch(
            "lifetracker.core.admin_views.idle_watchdog_page_context",
            side_effect=RuntimeError("boom"),
        ):
            response = client.get(reverse("admin:idle_watchdog"))
        self.assertEqual(response.status_code, 500)
        output = self.stream.getvalue()
        self.assertIn("ERROR django.request: Internal Server Error", output)
        self.assertIn("Traceback (most recent call last):", output)
        self.assertIn("RuntimeError: boom", output)

    def test_app_logger_logs_info(self) -> None:
        logging.getLogger("lifetracker.core.admin").info("hello")
        self.assertRegex(
            self.stream.getvalue(),
            r"\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} "
            r"INFO lifetracker\.core\.admin: hello\n\Z",
        )

    def test_runserver_request_log_keeps_its_own_handler(self) -> None:
        # Configuring "django" resets its children, so without its own entry
        # runserver's request log would propagate to the plain handler and lose
        # the colours ServerFormatter gives it.
        server_logger = logging.getLogger("django.server")
        self.assertFalse(server_logger.propagate)
        # pytest adds its capture handlers to non-propagating loggers too.
        (handler,) = (h for h in server_logger.handlers if type(h).__module__ != "_pytest.logging")
        self.assertIs(type(handler), logging.StreamHandler)
        self.assertIsInstance(handler.formatter, ServerFormatter)


class SettingsLogTimestampsTests(_SettingsSubprocessMixin, TestCase):
    """LIFETRACKER_LOG_TIMESTAMPS turns off the timestamp at the start of each
    log line, for Fly, which stamps every line itself."""

    EXPR = "s.LOGGING['formatters']['stderr']['format']"

    def _format(self, **env: str) -> str:
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env=env, expr=self.EXPR)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_on_by_default(self) -> None:
        self.assertEqual(self._format(), "{asctime} {levelname} {name}: {message}")

    def test_off(self) -> None:
        self.assertEqual(
            self._format(LIFETRACKER_LOG_TIMESTAMPS="0"),
            "{levelname} {name}: {message}",
        )

    def test_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env={"LIFETRACKER_LOG_TIMESTAMPS": "no"}, expr=self.EXPR)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("LIFETRACKER_LOG_TIMESTAMPS must be", proc.stderr)


class SQLiteFileDatabaseTests(TestCase):
    """The production DATABASES settings, applied to a file-backed database.

    The test database is in memory, where WAL mode doesn't apply, so these
    open a separate connection to a temporary file with the same settings.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "db.sqlite3"
        handler = ConnectionHandler({"default": {**settings.DATABASES["default"], "NAME": self.db_path}})
        self.addCleanup(handler.close_all)
        self.connection = handler["default"]

    def test_journal_mode_is_wal(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode")
            self.assertEqual(cursor.fetchone(), ("wal",))

    def test_backup_database_includes_uncheckpointed_wal(self) -> None:
        with self.connection.cursor() as cursor:
            # Stop SQLite copying the WAL back into the main file on commit,
            # so the row exists only in db.sqlite3-wal.
            cursor.execute("PRAGMA wal_autocheckpoint=0")
            cursor.execute("CREATE TABLE t (v TEXT)")
            cursor.execute("INSERT INTO t VALUES ('in the wal')")
        with mock.patch("lifetracker.core.backup.connections", {"default": self.connection}):
            with backup_database() as backup_file:
                data = backup_file.read()
        copy_path = self.db_path.with_name("copy.sqlite3")
        copy_path.write_bytes(data)
        copy = sqlite3.connect(copy_path)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT v FROM t").fetchall(), [("in the wal",)])

    def test_backup_database_refuses_inside_transaction(self) -> None:
        with mock.patch("lifetracker.core.backup.connections", {"default": self.connection}):
            with mock.patch.object(self.connection, "in_atomic_block", True):
                with self.assertRaisesMessage(RuntimeError, "Can't back up the database inside a transaction."):
                    backup_database()

    def test_backup_database_leaves_no_file_behind(self) -> None:
        with mock.patch("lifetracker.core.backup.connections", {"default": self.connection}):
            backup_file = backup_database()
        with backup_file:
            self.assertFalse(os.path.exists(backup_file.name))
            self.assertTrue(backup_file.read(16).startswith(b"SQLite format 3\x00"))
