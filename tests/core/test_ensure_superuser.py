import io
import os
import tempfile
import typing
from pathlib import Path
from typing import override
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from lifetracker.core.management.commands.ensure_superuser import _initial_admin_password_file


class InitialAdminPasswordFileTests(TestCase):
    """Where ensure_superuser looks for the initial admin password file."""

    def _resolve(self, env: dict[str, str] | None = None) -> Path:
        # patch.dict restores the whole environment afterwards, including the
        # pop, so a real LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE can't leak in.
        with (
            mock.patch.dict(os.environ),
            self.settings(LIFETRACKER_CONFIG_DIR=Path("/config")),
        ):
            os.environ.pop("LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE", None)
            os.environ.update(env or {})
            return _initial_admin_password_file()

    def test_defaults_to_config_dir(self) -> None:
        self.assertEqual(self._resolve(), Path("/config/initial_admin_password"))

    def test_env_var_overrides(self) -> None:
        self.assertEqual(
            self._resolve({"LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE": "/elsewhere/pw"}),
            Path("/elsewhere/pw"),
        )

    def test_empty_env_var_is_ignored(self) -> None:
        self.assertEqual(
            self._resolve({"LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE": ""}),
            Path("/config/initial_admin_password"),
        )


class EnsureSuperuserCommandTests(TestCase):
    """Tests for the ensure_superuser management command."""

    @override
    def setUp(self) -> None:
        # Point the initial-admin-password lookup at a file inside a fresh
        # temp dir that doesn't exist unless a test writes to it, so this
        # never picks up a real ~/.config/lifetracker/initial_admin_password.
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.password_file = Path(tmpdir.name) / "initial_admin_password"
        patcher = mock.patch(
            "lifetracker.core.management.commands.ensure_superuser.INITIAL_ADMIN_PASSWORD_FILE",
            self.password_file,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # Likewise a real LIFETRACKER_INITIAL_ADMIN_PASSWORD (e.g. from .env);
        # patch.dict restores the environment afterwards.
        env_patcher = mock.patch.dict(os.environ)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop("LIFETRACKER_INITIAL_ADMIN_PASSWORD", None)

    def _call(self, **kwargs: typing.Any) -> str:
        """Call ensure_superuser and return captured stdout."""
        out = io.StringIO()
        call_command("ensure_superuser", stdout=out, **kwargs)
        return out.getvalue()

    def test_creates_new_user_with_random_password(self) -> None:
        output = self._call(username="testadmin", email="a@example.com")
        self.assertTrue(User.objects.filter(username="testadmin").exists())
        user = User.objects.get(username="testadmin")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_active)
        self.assertIn("testadmin", output)
        # Password is the last word on the first output line, 32 hex chars.
        first_line = output.splitlines()[0]
        printed_password = first_line.split()[-1]
        self.assertEqual(len(printed_password), 32)
        self.assertTrue(user.check_password(printed_password))

    def test_default_username_is_admin(self) -> None:
        output = self._call()
        self.assertTrue(User.objects.filter(username="admin").exists())
        self.assertIn("admin", output)

    def test_creates_new_user_with_password_from_file(self) -> None:
        self.password_file.parent.mkdir(parents=True, exist_ok=True)
        self.password_file.write_text("from-the-file\n")
        output = self._call(username="admin")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("from-the-file"))
        # The password itself must never be printed.
        self.assertNotIn("from-the-file", output)
        self.assertIn(str(self.password_file), output)

    def test_creates_new_user_with_password_from_env(self) -> None:
        os.environ["LIFETRACKER_INITIAL_ADMIN_PASSWORD"] = " from-the-env\n"
        output = self._call(username="admin")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("from-the-env"))
        self.assertNotIn("from-the-env", output)
        self.assertIn("$LIFETRACKER_INITIAL_ADMIN_PASSWORD", output)

    def test_env_password_takes_precedence_over_file(self) -> None:
        self.password_file.write_text("from-the-file\n")
        os.environ["LIFETRACKER_INITIAL_ADMIN_PASSWORD"] = "from-the-env"
        self._call(username="admin")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("from-the-env"))

    def test_blank_env_password_falls_back_to_file(self) -> None:
        self.password_file.write_text("from-the-file\n")
        os.environ["LIFETRACKER_INITIAL_ADMIN_PASSWORD"] = "  "
        output = self._call(username="admin")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("from-the-file"))
        self.assertIn(str(self.password_file), output)

    def test_env_password_ignored_for_existing_user(self) -> None:
        User.objects.create_superuser("admin", "", "existingpassword")
        os.environ["LIFETRACKER_INITIAL_ADMIN_PASSWORD"] = "from-the-env"
        self._call(username="admin")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("existingpassword"))

    def test_empty_password_file_falls_back_to_random_password(self) -> None:
        self.password_file.parent.mkdir(parents=True, exist_ok=True)
        self.password_file.write_text("   \n")
        output = self._call(username="admin")
        user = User.objects.get(username="admin")
        first_line = output.splitlines()[0]
        printed_password = first_line.split()[-1]
        self.assertEqual(len(printed_password), 32)
        self.assertTrue(user.check_password(printed_password))

    def test_existing_user_no_change_when_already_superuser_with_password(self) -> None:
        User.objects.create_superuser("admin", "", "existingpassword")
        output = self._call(username="admin")
        # No password should be printed when user already has one
        self.assertEqual(output.strip(), "")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("existingpassword"))

    def test_existing_user_flags_repaired(self) -> None:
        user = User.objects.create_user("admin", "", "existingpassword")
        user.is_active = False
        user.is_staff = False
        user.is_superuser = False
        user.save()
        self._call(username="admin")
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        # Password unchanged — existing password should still work
        self.assertTrue(user.check_password("existingpassword"))

    def test_existing_user_password_left_alone_when_unusable(self) -> None:
        user = User.objects.create_superuser("admin", "", "tmp")
        user.set_unusable_password()
        user.save()
        output = self._call(username="admin")
        self.assertEqual(output.strip(), "")
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())
