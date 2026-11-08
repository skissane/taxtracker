from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.test import TestCase, override_settings
from django.urls import reverse


class WebManifestTests(TestCase):
    """The unauthenticated web app manifest, and the admin pages' links to it."""

    url = "/manifest.webmanifest"

    @override_settings(LIFETRACKER_UNAUTHENTICATED_SITE_NAME="Notes")
    def test_manifest(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        self.assertIn("max-age=3600", response["Cache-Control"])
        self.assertEqual(
            response.json(),
            {
                "id": "/",
                "name": "Notes",
                "short_name": "Notes",
                "scope": "/",
                "start_url": "/admin/",
                "display": "standalone",
                "background_color": "#417690",
                "theme_color": "#417690",
                "icons": [
                    {
                        "src": "/static/core/app_icon.png",
                        "sizes": "512x512",
                        "type": "image/png",
                    }
                ],
            },
        )

    @override_settings(LIFETRACKER_UNAUTHENTICATED_SITE_NAME="Notes")
    def test_logged_in_still_gets_unauthenticated_name(self) -> None:
        user = User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url).json()["name"], "Notes")

    def test_head_allowed_post_not(self) -> None:
        self.assertEqual(self.client.head(self.url).status_code, 200)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_icon_is_a_static_file(self) -> None:
        icon = finders.find("core/app_icon.png")
        assert isinstance(icon, str)
        with open(icon, "rb") as f:
            header = f.read(24)
        self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
        # Width and height, from the IHDR chunk.
        self.assertEqual(header[16:24], (512).to_bytes(4, "big") * 2)

    @override_settings(LIFETRACKER_UNAUTHENTICATED_SITE_NAME="Notes")
    def test_admin_pages_link_it(self) -> None:
        response = self.client.get(reverse("admin:login"))
        self.assertContains(response, '<link rel="manifest" href="/manifest.webmanifest">', html=True)
        self.assertContains(
            response,
            '<link rel="apple-touch-icon" href="/static/core/app_icon.png">',
            html=True,
        )
        self.assertContains(
            response,
            '<meta name="apple-mobile-web-app-title" content="Notes">',
            html=True,
        )
        user = User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(user)
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, '<link rel="manifest" href="/manifest.webmanifest">', html=True)
        self.assertContains(
            response,
            '<meta name="apple-mobile-web-app-title" content="Life Tracker">',
            html=True,
        )
