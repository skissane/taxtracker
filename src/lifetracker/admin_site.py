from django.conf import settings
from django.contrib import admin
from django.utils.html import format_html

# Git's default abbreviated object name length (core.abbrev's minimum), also what
# GitHub displays.
_SHORT_COMMIT_ID_LENGTH = 7

_SITE_NAME = "Life Tracker"


def _commit_url(commit_url_base, commit_id):
    return f"{commit_url_base.rstrip('/')}/{commit_id}"


def _build_admin_site_header(
    *, site_name, commit_id, commit_url_base, instance_name, link_commit
):
    if commit_id and link_commit:
        return format_html(
            '{} <code><a href="{}" target="_blank" rel="noopener noreferrer"'
            ' title="{}">{}</a></code> <code>{}</code>',
            site_name,
            _commit_url(commit_url_base, commit_id),
            commit_id,
            commit_id[:_SHORT_COMMIT_ID_LENGTH],
            instance_name,
        )
    if commit_id:
        return format_html(
            '{} <code title="{}">{}</code> <code>{}</code>',
            site_name,
            commit_id,
            commit_id[:_SHORT_COMMIT_ID_LENGTH],
            instance_name,
        )
    return format_html("{} <code>{}</code>", site_name, instance_name)


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
