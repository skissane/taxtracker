from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.utils.html import format_html
from django.views.generic import RedirectView

# Git's default abbreviated object name length (core.abbrev's minimum), also what
# GitHub displays.
_SHORT_COMMIT_ID_LENGTH = 7


def _commit_url(commit_url_base, commit_id):
    return f"{commit_url_base.rstrip('/')}/{commit_id}"


def _build_admin_site_header(*, commit_id, commit_url_base, instance_name):
    if commit_id:
        return format_html(
            'Life Tracker <code><a href="{}" target="_blank" rel="noopener noreferrer"'
            ' title="{}">{}</a></code> <code>{}</code>',
            _commit_url(commit_url_base, commit_id),
            commit_id,
            commit_id[:_SHORT_COMMIT_ID_LENGTH],
            instance_name,
        )
    return format_html("Life Tracker <code>{}</code>", instance_name)


admin.site.site_header = _build_admin_site_header(
    commit_id=settings.LIFETRACKER_COMMIT_ID,
    commit_url_base=settings.LIFETRACKER_COMMIT_URL_BASE,
    instance_name=settings.LIFETRACKER_BACKUP_HOSTNAME,
)
admin.site.site_title = "Life Tracker"
admin.site.index_title = "Administration"

urlpatterns = [
    # There's no non-admin site yet, so send the root URL to the admin. Temporary
    # (302) rather than permanent, so browsers don't cache it once there is one.
    path("", RedirectView.as_view(pattern_name="admin:index"), name="root"),
    path("google_sso/", include("django_google_sso.urls", namespace="django_google_sso")),
    path("admin/", admin.site.urls),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
