from django.contrib.admin.apps import AdminConfig


class LifetrackerAdminConfig(AdminConfig):
    """Replaces django.contrib.admin in INSTALLED_APPS, to make admin.site a
    LifetrackerAdminSite."""

    default_site = "lifetracker.admin_site.LifetrackerAdminSite"
