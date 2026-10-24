"""URLconf for Google SSO, replacing ``django_google_sso.urls``.

The library's own URLconf only registers its routes if ``GOOGLE_SSO_ENABLED`` is
true at import time, which ``override_settings`` can't change later. Instead, the
routes are always registered, and each view checks the setting per request and
404s when SSO is disabled (as if the route didn't exist, matching the library's
behaviour). Keep the routes, views and names in sync with ``django_google_sso.urls``;
a test checks this.

``start_login`` also drops a ``next`` outside the admin. The library treats any
other destination as a non-admin "page", and as ``GOOGLE_SSO_PAGES_ENABLED`` is off
its callback then fails with "Google SSO not enabled for Pages". The login
page's Google button passes on the page's own ``next``, which is ``/`` when the
proxy redirects a logged-out visit to the bare domain.
"""

from functools import wraps

from django.http import Http404
from django.urls import path, reverse
from django_google_sso import conf, views

app_name = "django_google_sso"


def _require_sso_enabled(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not conf.GOOGLE_SSO_ENABLED:
            raise Http404("Google SSO is not enabled.")
        return view(request, *args, **kwargs)

    return wrapper


def _admin_next_only(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        next_url = request.GET.get("next")
        if next_url is not None and not next_url.startswith(reverse("admin:index")):
            # Without it, the library goes to GOOGLE_SSO_NEXT_URL, the admin index.
            request.GET = request.GET.copy()
            del request.GET["next"]
        return view(request, *args, **kwargs)

    return wrapper


urlpatterns = [
    path(
        "login/",
        _require_sso_enabled(_admin_next_only(views.start_login)),
        name="oauth_start_login",
    ),
    path(
        "callback/",
        _require_sso_enabled(views.callback),
        name="oauth_callback",
    ),
]
