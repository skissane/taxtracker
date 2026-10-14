from __future__ import annotations

import os

from django.conf import settings
from django.http import HttpRequest


def _normalise_email(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def _iter_setting_values(*names):
    for name in names:
        value = getattr(settings, name, None)
        if value in (None, ""):
            continue
        if isinstance(value, str):
            yield from (item.strip() for item in value.split(",") if item.strip())
        else:
            yield from (str(item).strip() for item in value if str(item).strip())


def _allowed_google_emails():
    """Return the explicit Google email allowlist, if any.

    This app intentionally keeps Google SSO locked down until an operator adds an
    email address to the allowlist manually. That means no account can log in just
    because it exists in Django; only a deliberately configured email can match.
    """
    emails = set()
    for name in (
        "GOOGLE_SSO_ALLOWABLE_EMAILS",
        "GOOGLE_SSO_ALLOWED_EMAILS",
    ):
        emails.update(_normalise_email(item) for item in _iter_setting_values(name))
    for env_name in (
        "LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS",
        "LIFETRACKER_GOOGLE_SSO_ALLOWABLE_EMAILS",
        "GOOGLE_SSO_ALLOWED_EMAILS",
        "GOOGLE_SSO_ALLOWABLE_EMAILS",
    ):
        emails.update(
            _normalise_email(item)
            for item in os.environ.get(env_name, "").split(",")
            if item.strip()
        )
    return {email for email in emails if email}


def pre_validate_user(google_user_info: dict, request: HttpRequest) -> bool:
    """Allow only explicitly enabled Google identities to log in."""
    email = _normalise_email(google_user_info.get("email"))
    if not email:
        return False
    return email in _allowed_google_emails()


def pre_create_user(google_user_info: dict, request: HttpRequest) -> dict | None:
    """Disallow auto-creation of new users from Google SSO."""
    return {}


def pre_login_user(user, request: HttpRequest) -> None:
    """No-op hook used by the library during a successful login."""
    return None
