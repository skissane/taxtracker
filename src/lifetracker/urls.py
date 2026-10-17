from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    # There's no non-admin site yet, so send the root URL to the admin. Temporary
    # (302) rather than permanent, so browsers don't cache it once there is one.
    path("", RedirectView.as_view(pattern_name="admin:index"), name="root"),
    path(
        "google_sso/",
        include("lifetracker.google_sso_urls"),
    ),
    path("admin/", admin.site.urls),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
