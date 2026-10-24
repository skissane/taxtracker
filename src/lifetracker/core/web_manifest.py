"""The web app manifest, ``/manifest.webmanifest``.

It lets "Add to Home Screen" (iOS) and "Install app" (Android, desktop Chrome)
open the admin as a standalone app with its own name and icon. Browsers fetch it
without cookies, so it's on the auth whitelist, and it names the site as the
login page does to a visitor who isn't logged in
(``LIFETRACKER_UNAUTHENTICATED_SITE_NAME``) whoever asks. Hence a view rather
than a static file. ``templates/admin/base_site.html`` links it, and the icon,
from every admin page, the login page included.

The icon is ``core/static/core/app_icon.png``, rendered at 512x512 from
``app_icon.svg`` beside it. It's opaque, as iOS fills transparency with black.
It's served for every size, as iOS and Android scale it down themselves.
"""

from django.conf import settings
from django.http import JsonResponse
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_safe

# The admin's header background (its stylesheet's --header-bg).
THEME_COLOR = "#417690"
APP_ICON = "core/app_icon.png"


def manifest():
    """The manifest, as a dict."""
    name = settings.LIFETRACKER_UNAUTHENTICATED_SITE_NAME
    return {
        # Fixed, so a later change to start_url doesn't make it another app.
        "id": "/",
        "name": name,
        "short_name": name,
        "scope": "/",
        "start_url": reverse("admin:index"),
        "display": "standalone",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": static(APP_ICON), "sizes": "512x512", "type": "image/png"},
        ],
    }


@cache_control(max_age=3600)
@require_safe
def web_manifest(request):
    """The manifest as ``application/manifest+json``."""
    return JsonResponse(manifest(), content_type="application/manifest+json")
