from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from lifetracker.core.health import healthz
from lifetracker.core.web_manifest import web_manifest

urlpatterns = [
    # There's no non-admin site yet, so send the root URL to the admin. Temporary
    # (302) rather than permanent, so browsers don't cache it once there is one.
    path("", RedirectView.as_view(pattern_name="admin:index"), name="root"),
    path(
        "google_sso/",
        include("lifetracker.google_sso_urls"),
    ),
    path("admin/", admin.site.urls),
    # Unauthenticated, for CI's post-deploy check (see core/health.py).
    path("healthz/", healthz, name="healthz"),
    # Unauthenticated, as browsers fetch it without cookies (see
    # core/web_manifest.py).
    path("manifest.webmanifest", web_manifest, name="web_manifest"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
