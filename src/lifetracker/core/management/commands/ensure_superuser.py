"""Management command: ensure_superuser.

Creates or repairs a superuser account non-interactively.

Behaviour
---------
* ``--username``  (default: ``admin``) — the username to create/repair.
* ``--email``     (default: ``""``)   — email address (only used on creation).

If the user **does not exist**, a new superuser is created. Its initial
password is read from ``~/.config/lifetracker/initial_admin_password``
(stripped) if that file exists and is non-empty; in that case the password
is not printed, since it's assumed the operator already knows it. Otherwise
a random password (``secrets.token_hex(16)``) is generated and printed.

If the user **exists**:
  * ``is_active``, ``is_staff``, and ``is_superuser`` are all set to ``True``
    (and saved) if any of them are ``False``.
  * Its password is left untouched either way.
"""

import secrets
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

INITIAL_ADMIN_PASSWORD_FILE = (
    Path.home() / ".config" / "lifetracker" / "initial_admin_password"
)


class Command(BaseCommand):
    help = (
        "Ensure a superuser account exists. "
        "Creates or repairs the account non-interactively."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            default="admin",
            help="Username of the superuser (default: admin).",
        )
        parser.add_argument(
            "--email",
            default="",
            help="Email address (only used when creating a new user).",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"]
        email = options["email"]

        try:
            user = User.objects.get(**{User.USERNAME_FIELD: username})
        except User.DoesNotExist:
            initial_password = self._read_initial_password()
            if initial_password is not None:
                User.objects.create_superuser(
                    username=username, email=email, password=initial_password
                )
                self.stdout.write(
                    f"Created superuser '{username}' using password from "
                    f"{INITIAL_ADMIN_PASSWORD_FILE}."
                )
            else:
                password = secrets.token_hex(16)
                User.objects.create_superuser(
                    username=username, email=email, password=password
                )
                self.stdout.write(
                    f"Created superuser '{username}' with password: {password}"
                )
                self.stdout.write("Change this password after first login.")
            return

        # User exists – repair flags if needed. Password is left alone.
        if not user.is_active or not user.is_staff or not user.is_superuser:
            user.is_active = True
            user.is_staff = True
            user.is_superuser = True
            user.save()

    def _read_initial_password(self):
        if not INITIAL_ADMIN_PASSWORD_FILE.exists():
            return None
        password = INITIAL_ADMIN_PASSWORD_FILE.read_text().strip()
        return password or None
