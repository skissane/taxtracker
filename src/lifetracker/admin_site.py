from django.conf import settings
from django.contrib import admin
from django.utils.html import format_html

# Git's default abbreviated object name length (core.abbrev's minimum), also what
# GitHub displays.
_SHORT_COMMIT_ID_LENGTH = 7

_SITE_NAME = "Life Tracker"


def _commit_url(commit_url_base, commit_id):
    return f"{commit_url_base.rstrip('/')}/{commit_id}"


_LINK_COLOR = "#111827"
# Keep the background tint aligned with the text color so the header stays legible
# in both the light and dark admin themes.
_CODE_STYLE = (
    f"color: {_LINK_COLOR}; background-color: rgba(17, 24, 39, 0.08); "
    "border-radius: 0.25rem; padding: 0 0.25em;"
)


def _build_admin_site_header(
    *, site_name, commit_id, commit_url_base, instance_name, link_commit
):
    if commit_id and link_commit:
        return format_html(
            '{} <code style="{}"><a href="{}" target="_blank" rel="noopener noreferrer"'
            f' title="{{}}" style="color: {_LINK_COLOR};">{{}}</a></code> '
            '<code style="{}">{}</code>',
            site_name,
            _CODE_STYLE,
            _commit_url(commit_url_base, commit_id),
            commit_id,
            commit_id[:_SHORT_COMMIT_ID_LENGTH],
            _CODE_STYLE,
            instance_name,
        )
    if commit_id:
        return format_html(
            '{} <code style="{}" title="{}">{}</code> <code style="{}">{}</code>',
            site_name,
            _CODE_STYLE,
            commit_id,
            commit_id[:_SHORT_COMMIT_ID_LENGTH],
            _CODE_STYLE,
            instance_name,
        )
    return format_html(
        '{} <code style="{}">{}</code>',
        site_name,
        _CODE_STYLE,
        instance_name,
    )


class LifetrackerAdminSite(admin.AdminSite):
    """The admin site, installed as admin.site by LifetrackerAdminConfig. Its
    header and title depend on whether the visitor is logged in, so they're built
    per request rather than set once on the (shared) site object."""

    index_title = "Administration"
    # django_google_sso only sets this if SSO is enabled at import time. Set it
    # unconditionally: the template's tag checks per request whether SSO is
    # enabled, and renders the stock admin login (no Google button) when it isn't.
    login_template = "google_sso/login.html"

    def each_context(self, request):
        context = super().each_context(request)
        authenticated = request.user.is_authenticated
        site_name = (
            _SITE_NAME
            if authenticated
            else settings.LIFETRACKER_UNAUTHENTICATED_SITE_NAME
        )
        context["site_title"] = site_name
        context["site_header"] = _build_admin_site_header(
            site_name=site_name,
            commit_id=settings.LIFETRACKER_COMMIT_ID,
            commit_url_base=settings.LIFETRACKER_COMMIT_URL_BASE,
            instance_name=settings.LIFETRACKER_BACKUP_HOSTNAME,
            link_commit=authenticated,
        )
        return context
