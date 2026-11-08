from django.test import Client, TestCase
from django.urls import reverse


class RootRedirectTests(TestCase):
    def test_root_redirects_to_admin_index(self) -> None:
        response = Client().get("/")
        self.assertRedirects(
            response,
            reverse("admin:index"),
            status_code=302,
            fetch_redirect_response=False,
        )
