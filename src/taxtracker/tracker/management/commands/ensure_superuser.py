"""Management command: ensure_superuser.

Creates or repairs a superuser account non-interactively.

Behaviour
---------
* ``--username``  (default: ``admin``) — the username to create/repair.
* ``--email``     (default: ``""``)   — email address (only used on creation).

If the user **does not exist**, a new superuser is created with a randomly
generated password (``secrets.token_hex(16)``) and the password is printed.

If the user **exists**:
  * ``is_active``, ``is_staff``, and ``is_superuser`` are all set to ``True``
    (and saved) if any of them are ``False``.
  * If the user has **no usable password** (``has_usable_password()`` returns
    ``False``), a new random password is generated, set, and printed.
  * Otherwise nothing is changed and nothing is printed.
"""

import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


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
            # Create brand-new superuser with a random password.
            password = secrets.token_hex(16)
            User.objects.create_superuser(
                username=username, email=email, password=password
            )
            self.stdout.write(
                f"Created superuser '{username}' with password: {password}"
            )
            self.stdout.write(
                "Change this password after first login."
            )
            return

        # User exists – repair flags if needed.
        needs_save = False
        if not user.is_active or not user.is_staff or not user.is_superuser:
            user.is_active = True
            user.is_staff = True
            user.is_superuser = True
            needs_save = True

        # Set a password if the account has no usable password.
        if not user.has_usable_password():
            password = secrets.token_hex(16)
            user.set_password(password)
            needs_save = True
            self.stdout.write(
                f"Set password for existing user '{username}': {password}"
            )

        if needs_save:
            user.save()
