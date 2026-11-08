import re
from typing import override
from unittest import mock

from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from lifetracker.core.timing import REQUEST_START_ATTR
from lifetracker.taxtracker.models import FinancialYear


class RequestTimingTests(TestCase):
    PAGE_TIME = re.compile(r'<p class="lt-page-time">Page generated in \d+\.\d{3} seconds</p>')

    @override
    def setUp(self) -> None:
        User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client.login(username="admin", password="password")

    def assert_footer_shows_time(self, url: str) -> None:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertRegex(content, self.PAGE_TIME)
        self.assertIn(".lt-page-time {", content)

    def test_admin_pages_show_generation_time_in_footer(self) -> None:
        year = FinancialYear.objects.create(year=2024)
        for url in [
            reverse("admin:index"),
            reverse("admin:taxtracker_financialyear_changelist"),
            reverse("admin:taxtracker_financialyear_change", args=[year.pk]),
            reverse("admin:taxtracker_financialyear_summary", args=[year.pk]),
            reverse("admin:taxtracker_item_add"),
            reverse("admin:core_cloudbackupstatus_changelist"),
            reverse("admin:idle_watchdog"),
        ]:
            with self.subTest(url=url):
                self.assert_footer_shows_time(url)

    def test_login_page_shows_generation_time_in_footer(self) -> None:
        self.client.logout()
        self.assert_footer_shows_time(reverse("admin:login"))

    def test_server_timing_header(self) -> None:
        response = Client().get("/")
        self.assertRegex(response.headers["Server-Timing"], r"^app;dur=\d+\.\d$")

    def test_tag_measures_from_request_start(self) -> None:
        request = RequestFactory().get("/")
        template = Template("{% load page_timing %}{% page_generation_time %}")
        with mock.patch("time.perf_counter", return_value=12.5):
            setattr(request, REQUEST_START_ATTR, 10.0)
            rendered = template.render(Context({"request": request}))
        self.assertEqual(rendered, "2.500")

    def test_tag_is_empty_without_middleware_or_request(self) -> None:
        template = Template("{% load page_timing %}{% page_generation_time %}")
        self.assertEqual(template.render(Context({})), "")
        request = RequestFactory().get("/")
        self.assertEqual(template.render(Context({"request": request})), "")
