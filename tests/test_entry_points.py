from django.test import TestCase

from lifetracker.asgi import application as asgi_application
from lifetracker.wsgi import application as wsgi_application


class ProjectEntryPointTests(TestCase):
    """The asgi/wsgi entry-point modules are never imported by the app code
    itself (a real deployment imports them from outside the project), so
    nothing else exercises them."""

    def test_wsgi_application_is_importable(self) -> None:
        self.assertTrue(callable(wsgi_application))

    def test_asgi_application_is_importable(self) -> None:
        self.assertTrue(callable(asgi_application))
