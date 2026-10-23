"""Requests that need no login, for the authenticating front proxy.

The authenticating front proxy (issue #176), Envoy, lets these through without
the JWT that ``jwt_auth`` issues, so someone without a session can still log in.
Everything else needs one. ``envoy_config`` generates the proxy's rules from
``auth_whitelist()`` (the ``auth_whitelist`` management command prints it as
JSON), so they can't drift from the URLconf. ``jwt_auth.JWTCookieMiddleware``
checks the cookie on these too, but only for a session that has a token, which
it logs out if the cookie doesn't match; anyone else gets through.

Paths come from ``reverse()``, so they follow the URLconf. Keep the list to what
logging in and out needs, plus the health check.
"""

import dataclasses

from django.conf import settings
from django.urls import reverse

_GET = ("GET", "HEAD")


@dataclasses.dataclass(frozen=True)
class WhitelistEntry:
    """Requests with one of ``methods`` to ``path`` (or under it, if ``prefix``)."""

    path: str
    methods: tuple[str, ...]
    prefix: bool = False

    def matches(self, method, path):
        if method not in self.methods:
            return False
        if self.prefix:
            return path.startswith(self.path)
        return path == self.path


def auth_whitelist():
    """The ``WhitelistEntry`` list."""
    return [
        WhitelistEntry(reverse("admin:login"), (*_GET, "POST")),
        # Django's LogoutView only accepts POST.
        WhitelistEntry(reverse("admin:logout"), ("POST",)),
        WhitelistEntry(reverse("django_google_sso:oauth_start_login"), _GET),
        WhitelistEntry(reverse("django_google_sso:oauth_callback"), _GET),
        # The login page's CSS, JavaScript and Google logo. Every static file is
        # public anyway: they come from the repo and the installed packages,
        # and WhiteNoise serves them before any of this app's middleware runs.
        WhitelistEntry(settings.STATIC_URL, _GET, prefix=True),
        # CI's post-deploy check (core/health.py), which has no session.
        WhitelistEntry(reverse("healthz"), _GET),
    ]


def is_whitelisted(method, path):
    """Whether a ``method`` request to ``path`` (``request.path``) needs no login."""
    return any(entry.matches(method, path) for entry in auth_whitelist())
