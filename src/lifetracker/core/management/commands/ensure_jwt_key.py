"""Management command: ensure_jwt_key.

Creates the JWT signing key and its JWKS (see ``core/jwt_auth.py``) if they
don't exist yet, so the JWKS is there for the front proxy before the first
request. Run at startup by ``docker-entrypoint.sh`` and ``run.sh``.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from lifetracker.core.jwt_auth import signing_key


class Command(BaseCommand):
    help = "Create the JWT signing key and write its public key as a JWKS."

    def handle(self, *args, **options):
        signing_key()
        self.stdout.write(f"JWKS: {settings.LIFETRACKER_JWT_JWKS_FILE}")
