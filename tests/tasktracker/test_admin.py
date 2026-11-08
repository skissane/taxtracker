from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from lifetracker.tasktracker.admin import TaskTypeAdmin
from lifetracker.tasktracker.models import TaskType


class TaskTypeAdminGenericProtectionTests(TestCase):
    """The built-in 'Generic' TaskType (pk=1) should be uneditable and
    undeletable through the admin."""

    def setUp(self) -> None:
        self.admin = TaskTypeAdmin(TaskType, admin.site)
        self.generic = TaskType.objects.get(pk=TaskType.GENERIC_PK)
        self.other = TaskType.objects.create(name="Household")
        superuser = User.objects.create_superuser("root", "", "password")
        self.request = RequestFactory().get("/")
        self.request.user = superuser

    def test_delete_permission_denied_for_generic(self) -> None:
        self.assertFalse(self.admin.has_delete_permission(self.request, self.generic))

    def test_delete_permission_allowed_for_other(self) -> None:
        self.assertTrue(self.admin.has_delete_permission(self.request, self.other))

    def test_delete_permission_allowed_with_no_object(self) -> None:
        self.assertTrue(self.admin.has_delete_permission(self.request, None))

    def test_readonly_fields_locked_for_generic(self) -> None:
        readonly_fields = self.admin.get_readonly_fields(self.request, self.generic)
        self.assertIn("name", readonly_fields)
        self.assertIn("content_type", readonly_fields)

    def test_readonly_fields_unlocked_for_other(self) -> None:
        readonly_fields = self.admin.get_readonly_fields(self.request, self.other)
        self.assertNotIn("name", readonly_fields)
        self.assertNotIn("content_type", readonly_fields)

    def test_readonly_fields_unlocked_for_new_object(self) -> None:
        readonly_fields = self.admin.get_readonly_fields(self.request, None)
        self.assertNotIn("name", readonly_fields)
