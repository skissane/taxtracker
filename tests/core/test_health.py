from unittest import mock

from django.db import DatabaseError
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, override_settings


class HealthCheckTests(TestCase):
    """The unauthenticated /healthz/ endpoint CI checks after a deploy."""

    url = "/healthz/"

    @override_settings(LIFETRACKER_COMMIT_ID="abc123")
    def test_healthy(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "commit": "abc123"})
        self.assertIn("no-cache", response["Cache-Control"])
        self.assertNotIn("Set-Cookie", response.headers)

    def test_head_allowed_post_not(self) -> None:
        self.assertEqual(self.client.head(self.url).status_code, 200)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    @override_settings(LIFETRACKER_COMMIT_ID="abc123")
    def test_unapplied_migrations(self) -> None:
        migration = mock.Mock(app_label="core", name="0099_new")
        migration.name = "0099_new"
        with (
            mock.patch.object(MigrationExecutor, "migration_plan", return_value=[(migration, False)]),
            self.assertLogs("lifetracker.core.health", "ERROR") as logs,
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"status": "error", "problem": "unapplied migrations", "commit": "abc123"},
        )
        self.assertIn("core.0099_new", logs.output[0])

    def test_database_error(self) -> None:
        with (
            mock.patch.object(
                MigrationExecutor,
                "__init__",
                side_effect=DatabaseError("disk I/O error"),
            ),
            self.assertLogs("lifetracker.core.health", "ERROR") as logs,
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["problem"], "database error")
        self.assertNotIn("disk I/O", response.content.decode())
        self.assertIn("disk I/O error", logs.output[0])

    def test_other_error(self) -> None:
        with (
            mock.patch.object(MigrationExecutor, "__init__", side_effect=RuntimeError("boom")),
            self.assertLogs("lifetracker.core.health", "ERROR") as logs,
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["problem"], "unhealthy")
        self.assertIn("boom", logs.output[0])
