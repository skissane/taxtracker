"""Management command: envoy_config.

Writes Envoy's config, the template ``envoy/envoy.yaml`` filled in by
``core.envoy_config.render``, creating the JWT signing key and its JWKS first
if they don't exist yet (as ``ensure_jwt_key`` does), since Envoy reads the
JWKS at startup. Run by ``docker-entrypoint.sh`` before it starts Envoy.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from lifetracker.core import envoy_config
from lifetracker.core.jwt_auth import signing_key


class Command(BaseCommand):
    help = "Write Envoy's config, filled in from the settings and URLconf."

    def add_arguments(self, parser):
        parser.add_argument("template", type=Path, help="envoy/envoy.yaml")
        parser.add_argument("output", type=Path, help="Where to write the config.")
        parser.add_argument(
            "--listen-host", required=True, help="The IP address Envoy listens on."
        )
        parser.add_argument(
            "--listen-port", type=int, required=True, help="Envoy's port."
        )
        parser.add_argument(
            "--upstream-port",
            type=int,
            required=True,
            help="Gunicorn's port on 127.0.0.1.",
        )

    def handle(
        self, *args, template, output, listen_host, listen_port, upstream_port, **opts
    ):
        try:
            config = envoy_config.render(
                template.read_text(encoding="utf-8"),
                listen_host=listen_host,
                listen_port=listen_port,
                upstream_port=upstream_port,
            )
        except (OSError, ValueError) as e:
            raise CommandError(str(e)) from e
        signing_key()
        output.write_text(config, encoding="utf-8")
