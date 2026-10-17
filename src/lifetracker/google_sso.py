from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpRequest


def _normalise_email(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def _allowed_google_emails():
    """Return the explicit Google email allowlist.

    This app intentionally keeps Google SSO locked down until an operator adds an
    email address to the allowlist manually. That means no account can log in just
    because it exists in Django; only a deliberately configured email can match.
    """
    emails = map(_normalise_email, settings.LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS)
    return {email for email in emails if email}


def _email_is_ambiguous(email):
    """Return whether more than one Django user has this email.

    With auto-create off, the library looks the user up by email with ``.get()``,
    which would raise (a 500 error) if Django's non-unique email field matched
    several users.
    """
    user_model = get_user_model()
    lookup = {f"{user_model.get_email_field_name()}__iexact": email}
    return user_model.objects.filter(**lookup)[:2].count() > 1


def pre_validate_user(google_user_info: dict, request: HttpRequest) -> bool:
    """Allow only verified, explicitly allowlisted Google emails to log in."""
    email = _normalise_email(google_user_info.get("email"))
    # The v2 userinfo endpoint the library calls reports this as "verified_email".
    if not email or google_user_info.get("verified_email") is not True:
        return False
    return email in _allowed_google_emails() and not _email_is_ambiguous(email)
