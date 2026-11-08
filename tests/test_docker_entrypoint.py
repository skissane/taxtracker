import os
import subprocess
import tempfile
from pathlib import Path

from django.test import TestCase


class DockerEntrypointTests(TestCase):
    """Tests for the Docker entrypoint startup sequence."""

    STARTUP = [
        "python manage.py migrate",
        "python manage.py ensure_superuser",
    ]

    ENTRYPOINT_ENV = {
        "LIFETRACKER_GUNICORN_PORT",
        "LIFETRACKER_GUNICORN_THREADS",
        "LIFETRACKER_GUNICORN_TIMEOUT",
        "LIFETRACKER_HOST",
        "PORT",
        "TMPDIR",
    }

    # Each fake logs its name and arguments, one invocation per line.
    LOG_COMMAND = (
        "{\n"
        "  printf '%s' {name}\n"
        '  for arg in "$@"; do\n'
        "    printf ' %s' \"$arg\"\n"
        "  done\n"
        "  printf '\\n'\n"
        '} >> "$COMMANDS_LOG_PATH"\n'
    )

    # The entrypoint starts Envoy in the background just before execing
    # gunicorn, so the fakes would race. The fake Envoy then blocks, as the
    # real one does, and the fake gunicorn waits (up to 10 seconds) until
    # Envoy has logged, then logs and kills the rest of the entrypoint's
    # process group (Envoy and the loop that would restart it), which it runs
    # in a session of its own, ignoring the signal itself.
    FAKES = {
        "python": LOG_COMMAND + "exit 0\n",
        "envoy": LOG_COMMAND + "exec sleep 60\n",
        "gunicorn": (
            "i=0\n"
            'while ! grep -q "^envoy" "$COMMANDS_LOG_PATH" && [ "$i" -lt 100 ]; do\n'
            "  sleep 0.1\n"
            "  i=$((i + 1))\n"
            "done\n" + LOG_COMMAND + "trap '' TERM\n"
            "kill -TERM 0\n"
            "exit 0\n"
        ),
    }

    def _run_entrypoint(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        returncode: int = 0,
        fakes: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
        """Run the entrypoint with fake `python`, `envoy` and `gunicorn` on PATH.

        Returns the completed process, the logged lines and the path the
        entrypoint was to write Envoy's config to.
        """
        repo_root = next(parent for parent in Path(__file__).resolve().parents if (parent / "manage.py").exists())
        script = repo_root / "docker-entrypoint.sh"

        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            log_path = temp_path / "commands.log"
            log_path.touch()
            for name, body in {**self.FAKES, **(fakes or {})}.items():
                fake_path = temp_path / name
                fake_path.write_text("#!/usr/bin/env sh\n" + body.replace("{name}", name))
                fake_path.chmod(0o755)

            result = subprocess.run(
                ["sh", str(script), *args],
                cwd=repo_root,
                env={
                    # Drop the entrypoint's own settings so the caller's shell
                    # can't change what the defaults look like.
                    **{key: value for key, value in os.environ.items() if key not in self.ENTRYPOINT_ENV},
                    "PATH": f"{tempdir}:{os.environ['PATH']}",
                    "COMMANDS_LOG_PATH": str(log_path),
                    "LIFETRACKER_APP_DIR": str(repo_root),
                    "TMPDIR": tempdir,
                    **(env or {}),
                },
                check=False,
                capture_output=True,
                text=True,
                start_new_session=True,
                timeout=30,
            )
            log_lines = log_path.read_text().splitlines()

        self.assertEqual(result.returncode, returncode, result.stderr)
        return result, log_lines, str(temp_path / "lifetracker-envoy.yaml")

    @staticmethod
    def _envoy_config_command(config_path: str, host: str = "0.0.0.0", port: int = 8000, upstream: int = 8001) -> str:
        return (
            f"python manage.py envoy_config envoy/envoy.yaml {config_path}"
            f" --listen-host {host} --listen-port {port} --upstream-port {upstream}"
        )

    def test_entrypoint_runs_startup_commands_before_execing_arguments(self) -> None:
        result, log_lines, _ = self._run_entrypoint("sh", "-c", "printf ready")

        self.assertEqual(result.stdout, "ready")
        self.assertEqual(log_lines, [*self.STARTUP, "python manage.py ensure_jwt_key"])

    def test_entrypoint_starts_envoy_and_gunicorn_by_default(self) -> None:
        _, log_lines, envoy_config_path = self._run_entrypoint()

        self.assertEqual(
            log_lines,
            [
                *self.STARTUP,
                self._envoy_config_command(envoy_config_path),
                f"envoy --config-path {envoy_config_path} --concurrency 1 --disable-hot-restart --log-level warn",
                "gunicorn --config python:lifetracker.gunicorn_conf"
                " --worker-class gthread --threads 8"
                " --access-logfile - --error-logfile - --timeout 120"
                " --bind 127.0.0.1:8001 lifetracker.wsgi",
            ],
        )

    def test_entrypoint_settings_come_from_env(self) -> None:
        _, log_lines, envoy_config_path = self._run_entrypoint(
            env={
                "LIFETRACKER_GUNICORN_PORT": "9001",
                "LIFETRACKER_GUNICORN_THREADS": "2",
                "LIFETRACKER_GUNICORN_TIMEOUT": "600",
                "LIFETRACKER_HOST": "127.0.0.1",
                "PORT": "9000",
            }
        )

        self.assertEqual(
            log_lines[-1],
            "gunicorn --config python:lifetracker.gunicorn_conf"
            " --worker-class gthread --threads 2"
            " --access-logfile - --error-logfile - --timeout 600"
            " --bind 127.0.0.1:9001 lifetracker.wsgi",
        )
        self.assertEqual(
            log_lines[len(self.STARTUP)],
            self._envoy_config_command(envoy_config_path, "127.0.0.1", 9000, 9001),
        )

    def test_entrypoint_stops_gunicorn_when_envoy_keeps_failing(self) -> None:
        # Envoy exits at once every time. Gunicorn waits (up to 20 seconds)
        # for SIGTERM, then logs whether the failure file exists.
        result, log_lines, envoy_config_path = self._run_entrypoint(
            fakes={
                "envoy": self.LOG_COMMAND + "exit 3\n",
                "gunicorn": (
                    self.LOG_COMMAND + 'trap \'if [ -e "$LIFETRACKER_ENVOY_FAILED_FILE" ]; then'
                    ' echo gunicorn TERM, failed file >> "$COMMANDS_LOG_PATH"; fi;'
                    " exit 0' TERM\n"
                    "i=0\n"
                    'while [ "$i" -lt 200 ]; do\n'
                    "  sleep 0.1\n"
                    "  i=$((i + 1))\n"
                    "done\n"
                    "exit 1\n"
                ),
            }
        )

        self.assertEqual(
            [line for line in log_lines if line.startswith("envoy ")],
            [f"envoy --config-path {envoy_config_path} --concurrency 1 --disable-hot-restart --log-level warn"] * 5,
        )
        self.assertEqual(log_lines[-1], "gunicorn TERM, failed file")
        self.assertEqual(
            result.stderr.count("docker-entrypoint.sh: envoy exited with status 3\n"),
            5,
        )
        self.assertIn(
            "docker-entrypoint.sh: envoy exited within a minute of starting 5 times in a row; stopping gunicorn\n",
            result.stderr,
        )

    def test_entrypoint_stops_if_envoy_config_fails(self) -> None:
        _, log_lines, envoy_config_path = self._run_entrypoint(
            fakes={"python": self.LOG_COMMAND + '[ "$2" != envoy_config ] || exit 1\n'},
            returncode=1,
        )
        self.assertEqual(log_lines, [*self.STARTUP, self._envoy_config_command(envoy_config_path)])
