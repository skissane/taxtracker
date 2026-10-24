"""The app icon: uploads checked and stored, served publicly, and linked."""

import hashlib
import zlib

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from lifetracker.core import app_icon
from lifetracker.core.auth_whitelist import is_whitelisted
from lifetracker.core.models import AppIcon, DBStoredFile


def _chunk(chunk_type, body):
    crc = zlib.crc32(chunk_type + body).to_bytes(4, "big")
    return len(body).to_bytes(4, "big") + chunk_type + body + crc


def make_png(width, height=None, color_type=2, trns=False, pixel=b"\x00"):
    """A valid PNG of one colour; ``color_type`` 2 is RGB, 6 RGB + alpha."""
    height = width if height is None else height
    channels = {2: 3, 6: 4}[color_type]
    row = b"\x00" + pixel * (channels * width)
    header = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes([8, color_type, 0, 0, 0])
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + (_chunk(b"tRNS", b"\x00\x00\x00\x00\x00\x00") if trns else b"")
        + _chunk(b"IDAT", zlib.compress(row * height))
        + _chunk(b"IEND", b"")
    )


class PngCheckTests(SimpleTestCase):
    def test_valid(self):
        self.assertEqual(
            app_icon.check_icon(make_png(512)),
            app_icon.PngInfo(512, 512, has_transparency=False),
        )

    def test_transparency(self):
        self.assertTrue(app_icon.png_info(make_png(4, color_type=6)).has_transparency)
        self.assertTrue(app_icon.png_info(make_png(4, trns=True)).has_transparency)

    def test_invalid(self):
        png = make_png(512)
        for data, message in [
            (b"GIF89a" + png, "isn't a PNG"),
            (png[:40], "cut off"),
            (png[:-12], "cut off"),
            (png[:8] + _chunk(b"IDAT", b"") + png[8:], "no valid header"),
            (make_png(600, 512), "600x512, not square"),
            (make_png(511), "smaller than 512x512"),
            (png + b"\x00" * app_icon.MAX_BYTES, "bigger than 1024 KiB"),
        ]:
            with (
                self.subTest(message),
                self.assertRaisesMessage(app_icon.InvalidIcon, message),
            ):
                app_icon.check_icon(data)


class AppIconTests(TestCase):
    """Saving, resetting and serving the icon, and what links to it."""

    def test_default(self):
        icon = app_icon.current_icon()
        self.assertEqual(icon.url, "/static/core/app_icon.png")
        self.assertEqual((icon.width, icon.height), (512, 512))
        self.assertEqual(icon.favicon_url, "/static/core/app_icon.svg")
        self.assertEqual(icon.favicon_type, "image/svg+xml")
        self.assertIsNone(icon.uploaded_at)

    def test_save_replace_and_reset(self):
        first = make_png(512)
        app_icon.save_icon(first)
        stored = AppIcon.objects.get()
        self.assertEqual(stored.sha256, hashlib.sha256(first).hexdigest())
        icon = app_icon.current_icon()
        self.assertEqual(icon.url, f"/app-icon.png?v={stored.sha256[:16]}")
        self.assertEqual(icon.favicon_url, icon.url)
        self.assertEqual(icon.favicon_type, "image/png")
        self.assertIsNotNone(icon.uploaded_at)

        second = make_png(1024, pixel=b"\xff")
        app_icon.save_icon(second)
        self.assertEqual(AppIcon.objects.get().width, 1024)
        # The first one's file went with its row.
        self.assertEqual(
            list(DBStoredFile.objects.values_list("content", flat=True)), [second]
        )

        app_icon.reset_icon()
        self.assertFalse(AppIcon.objects.exists())
        self.assertFalse(DBStoredFile.objects.exists())
        self.assertIsNone(app_icon.current_icon().uploaded_at)

    def test_invalid_upload_changes_nothing(self):
        app_icon.save_icon(make_png(512))
        with self.assertRaises(app_icon.InvalidIcon):
            app_icon.save_icon(make_png(100))
        self.assertEqual(AppIcon.objects.get().width, 512)
        self.assertEqual(DBStoredFile.objects.count(), 1)

    def test_view_redirects_to_default(self):
        response = self.client.get("/app-icon.png")
        self.assertRedirects(
            response, "/static/core/app_icon.png", fetch_redirect_response=False
        )
        self.assertIn("no-cache", response["Cache-Control"])

    def test_view_serves_upload(self):
        data = make_png(512)
        app_icon.save_icon(data)
        url = app_icon.current_icon().url
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response.content, data)
        self.assertIn("max-age=31536000", response["Cache-Control"])
        self.assertIn("immutable", response["Cache-Control"])
        self.assertNotIn("Set-Cookie", response.headers)
        # Any other version (an old one, or none) mustn't be cached as this one.
        for other in ("/app-icon.png", "/app-icon.png?v=0123456789abcdef"):
            with self.subTest(other):
                response = self.client.get(other)
                self.assertEqual(response.content, data)
                self.assertIn("no-cache", response["Cache-Control"])
        self.assertEqual(self.client.head(url).status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 405)

    def test_whitelisted(self):
        self.assertTrue(is_whitelisted("GET", "/app-icon.png"))
        self.assertTrue(is_whitelisted("HEAD", "/app-icon.png"))
        self.assertFalse(is_whitelisted("POST", "/app-icon.png"))

    def test_manifest_and_head_use_upload(self):
        app_icon.save_icon(make_png(1024))
        url = app_icon.current_icon().url
        self.assertEqual(
            self.client.get("/manifest.webmanifest").json()["icons"],
            [{"src": url, "sizes": "1024x1024", "type": "image/png"}],
        )
        response = self.client.get(reverse("admin:login"))
        self.assertContains(
            response, f'<link rel="apple-touch-icon" href="{url}">', html=True
        )
        self.assertContains(
            response, f'<link rel="icon" type="image/png" href="{url}">', html=True
        )


class AppIconPageTests(TestCase):
    """The superuser-only App Icon admin page."""

    url = "/admin/app-icon/"

    def setUp(self):
        self.admin = User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(self.admin)

    def _upload(self, data, **extra):
        return self.client.post(
            self.url,
            {"image": SimpleUploadedFile("icon.png", data, "image/png")},
            **extra,
        )

    def test_superuser_only(self):
        staff = User.objects.create_user("staff", password="pw", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_top_bar_link(self):
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, f'<a href="{self.url}">App Icon</a>', html=True)

    def test_shows_default(self):
        response = self.client.get(self.url)
        self.assertContains(response, "The default")
        self.assertContains(response, 'src="/static/core/app_icon.png"')
        self.assertNotContains(response, "Reset to Default")

    def test_upload(self):
        response = self._upload(make_png(512), follow=True)
        self.assertRedirects(response, self.url)
        self.assertContains(response, "The new app icon is in place.")
        self.assertNotContains(response, "transparency, which")
        self.assertContains(response, f'src="{app_icon.current_icon().url}"')
        self.assertContains(response, "Reset to Default")

    def test_upload_with_transparency_warns(self):
        response = self._upload(make_png(512, color_type=6), follow=True)
        self.assertTrue(AppIcon.objects.exists())
        self.assertContains(response, "which iOS fills in with black.")

    def test_invalid_upload(self):
        response = self._upload(make_png(256))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "smaller than 512x512")
        self.assertFalse(AppIcon.objects.exists())

    def test_big_upload_refused(self):
        response = self._upload(make_png(512) + b"\x00" * (2 * app_icon.MAX_BYTES))
        self.assertContains(response, "bigger than 1024 KiB")
        self.assertFalse(AppIcon.objects.exists())

    def test_reset(self):
        app_icon.save_icon(make_png(512))
        response = self.client.post(self.url, {"reset": "1"}, follow=True)
        self.assertRedirects(response, self.url)
        self.assertContains(response, "The app icon is the default again.")
        self.assertFalse(AppIcon.objects.exists())
        self.assertFalse(DBStoredFile.objects.exists())
