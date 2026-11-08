import typing

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from lifetracker.admin_site import _TEXT_STYLE, LifetrackerAdminSite, _build_admin_site_header

if typing.TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse


class AdminSiteHeaderTests(TestCase):
    COMMIT_ID = "0123abc456def7890123abc456def7890123abcd"

    def test_includes_commit_link_and_instance_name(self) -> None:
        self.assertEqual(
            str(
                _build_admin_site_header(
                    site_name="Life Tracker",
                    link_commit=True,
                    commit_id=self.COMMIT_ID,
                    commit_url_base="https://github.com/skissane/taxtracker/commit/",
                    instance_name="instance.local",
                )
            ),
            (
                f'Life Tracker <code style="{_TEXT_STYLE}"><a'
                f' href="https://github.com/skissane/taxtracker/commit/{self.COMMIT_ID}"'
                ' target="_blank" rel="noopener noreferrer"'
                f' title="{self.COMMIT_ID}" style="{_TEXT_STYLE}">0123abc</a></code>'
                f' <code style="{_TEXT_STYLE}">instance.local</code>'
            ),
        )

    def test_omits_commit_link_when_no_commit_id(self) -> None:
        self.assertEqual(
            str(
                _build_admin_site_header(
                    site_name="Life Tracker",
                    link_commit=True,
                    commit_id="",
                    commit_url_base="https://github.com/skissane/taxtracker/commit/",
                    instance_name="instance.local",
                )
            ),
            f'Life Tracker <code style="{_TEXT_STYLE}">instance.local</code>',
        )

    def test_uses_site_name_color(self) -> None:
        # The other tests compare against the constant, so pin its value here.
        self.assertEqual(_TEXT_STYLE, "color: var(--header-branding-color);")

    def test_accepts_base_url_without_trailing_slash(self) -> None:
        self.assertEqual(
            str(
                _build_admin_site_header(
                    site_name="Life Tracker",
                    link_commit=True,
                    commit_id=self.COMMIT_ID,
                    commit_url_base="https://github.com/skissane/taxtracker/commit",
                    instance_name="instance.local",
                )
            ),
            (
                f'Life Tracker <code style="{_TEXT_STYLE}"><a'
                f' href="https://github.com/skissane/taxtracker/commit/{self.COMMIT_ID}"'
                ' target="_blank" rel="noopener noreferrer"'
                f' title="{self.COMMIT_ID}" style="{_TEXT_STYLE}">0123abc</a></code>'
                f' <code style="{_TEXT_STYLE}">instance.local</code>'
            ),
        )

    def test_omits_commit_link_when_not_linking_commit(self) -> None:
        self.assertEqual(
            str(
                _build_admin_site_header(
                    site_name="Example",
                    link_commit=False,
                    commit_id=self.COMMIT_ID,
                    commit_url_base="https://github.com/skissane/taxtracker/commit/",
                    instance_name="instance.local",
                )
            ),
            (
                f'Example <code style="{_TEXT_STYLE}" title="{self.COMMIT_ID}">0123abc'
                f'</code> <code style="{_TEXT_STYLE}">instance.local</code>'
            ),
        )

    def test_escapes_site_name(self) -> None:
        self.assertEqual(
            str(
                _build_admin_site_header(
                    site_name="<b>",
                    link_commit=False,
                    commit_id="",
                    commit_url_base="https://github.com/skissane/taxtracker/commit/",
                    instance_name="instance.local",
                )
            ),
            f'&lt;b&gt; <code style="{_TEXT_STYLE}">instance.local</code>',
        )


@override_settings(
    LIFETRACKER_COMMIT_ID=AdminSiteHeaderTests.COMMIT_ID,
    LIFETRACKER_COMMIT_URL_BASE="https://example.invalid/commit/",
    LIFETRACKER_BACKUP_HOSTNAME="instance.local",
    LIFETRACKER_UNAUTHENTICATED_SITE_NAME="Example Site",
)
class AdminSitePerRequestHeaderTests(TestCase):
    COMMIT_URL = f"https://example.invalid/commit/{AdminSiteHeaderTests.COMMIT_ID}"

    def test_admin_site_is_lifetracker_admin_site(self) -> None:
        self.assertIsInstance(admin.site, LifetrackerAdminSite)

    def test_logged_out_uses_unauthenticated_name_without_commit_link(self) -> None:
        response = Client().get(reverse("admin:login"))
        self.assertContains(response, "<title>Log in | Example Site</title>", html=True)
        self.assertContains(
            response,
            f'Example Site <code style="{_TEXT_STYLE}" '
            f'title="{AdminSiteHeaderTests.COMMIT_ID}">0123abc'
            f'</code> <code style="{_TEXT_STYLE}">instance.local</code>',
        )
        self.assertNotContains(response, self.COMMIT_URL)
        self.assertNotContains(response, "Life Tracker")

    def test_logged_in_uses_site_name_with_commit_link(self) -> None:
        client = Client()
        client.force_login(User.objects.create_superuser("admin", "", "pw"))
        response = client.get(reverse("admin:index"))
        self.assertContains(response, "<title>Administration | Life Tracker</title>", html=True)
        self.assertContains(
            response,
            f'Life Tracker <code style="{_TEXT_STYLE}"><a href="{self.COMMIT_URL}"',
        )
        self.assertNotContains(response, "Example Site")


class AdminSiteFlyMachineLinkTests(TestCase):
    URL = "https://fly.io/apps/example-app/machines/0123456789abcd"
    LINK = f'<a href="{URL}" target="_blank" rel="noopener noreferrer">Fly Machine</a>'

    def _index(self, user: User) -> TestResponse:
        client = Client()
        client.force_login(user)
        return client.get(reverse("admin:index"))

    @override_settings(LIFETRACKER_FLY_MACHINE_URL=URL)
    def test_top_bar_links_to_fly_machine_for_superusers(self) -> None:
        response = self._index(User.objects.create_superuser("admin", "", "pw"))
        self.assertContains(response, self.LINK, html=True)

    @override_settings(LIFETRACKER_FLY_MACHINE_URL=URL)
    def test_no_link_for_staff_or_logged_out(self) -> None:
        staff = User.objects.create_user("staff", "", "pw", is_staff=True)
        self.assertNotContains(self._index(staff), self.URL)
        self.assertNotContains(Client().get(reverse("admin:login")), self.URL)

    @override_settings(LIFETRACKER_FLY_MACHINE_URL="")
    def test_no_link_when_not_on_fly(self) -> None:
        response = self._index(User.objects.create_superuser("admin", "", "pw"))
        self.assertNotContains(response, "Fly Machine")
