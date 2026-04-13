import datetime
import io
import zipfile

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import FinancialYear, Item


class FinancialYearModelTests(TestCase):
    def setUp(self):
        self.fy = FinancialYear.objects.create(year=2024)

    def test_str(self):
        self.assertEqual(str(self.fy), "FY2024")

    def test_start_date(self):
        self.assertEqual(self.fy.start_date, datetime.date(2023, 7, 1))

    def test_end_date(self):
        self.assertEqual(self.fy.end_date, datetime.date(2024, 6, 30))

    def test_default_lodgement_date(self):
        self.assertEqual(self.fy.default_lodgement_date, datetime.date(2024, 10, 31))

    def test_effective_lodgement_date_default(self):
        self.assertEqual(self.fy.effective_lodgement_date, datetime.date(2024, 10, 31))

    def test_effective_lodgement_date_override(self):
        override = datetime.date(2024, 12, 31)
        self.fy.lodgement_date_override = override
        self.fy.save()
        self.assertEqual(self.fy.effective_lodgement_date, override)

    def test_days_until_lodgement(self):
        today = timezone.now().date()
        expected = (self.fy.effective_lodgement_date - today).days
        self.assertEqual(self.fy.days_until_lodgement, expected)

    def test_ordering(self):
        fy2025 = FinancialYear.objects.create(year=2025)
        fy2023 = FinancialYear.objects.create(year=2023)
        years = list(FinancialYear.objects.values_list("year", flat=True))
        # Default ordering is descending by year.
        self.assertEqual(years[0], 2025)
        self.assertIn(2024, years)
        self.assertIn(2023, years)
        _ = fy2025, fy2023  # satisfy linter


class ItemModelTests(TestCase):
    def setUp(self):
        self.fy = FinancialYear.objects.create(year=2024)
        self.parent = Item.objects.create(year=self.fy, title="Income", order=1)
        self.child = Item.objects.create(
            year=self.fy, parent=self.parent, title="Salary", order=1
        )

    def test_str_root(self):
        self.assertEqual(str(self.parent), "Income")

    def test_str_child(self):
        self.assertEqual(str(self.child), "Income > Salary")

    def test_is_done_false(self):
        self.assertFalse(self.parent.is_done)

    def test_is_done_true(self):
        self.parent.status = Item.STATUS_DONE
        self.assertTrue(self.parent.is_done)

    def test_get_folder_path_root(self):
        self.assertEqual(self.parent.get_folder_path(), ["Income"])

    def test_get_folder_path_child(self):
        self.assertEqual(self.child.get_folder_path(), ["Income", "Salary"])


class CopyItemsTests(TestCase):
    def setUp(self):
        self.fy = FinancialYear.objects.create(year=2024)
        self.root = Item.objects.create(
            year=self.fy,
            title="Root",
            order=1,
            notes="some note",
            status=Item.STATUS_DONE,
        )
        self.child = Item.objects.create(
            year=self.fy, parent=self.root, title="Child", order=1
        )

    def _create_superuser_client(self):
        User.objects.create_superuser("admin", "admin@example.com", "password")
        client = Client()
        client.login(username="admin", password="password")
        return client

    def test_copy_to_new_year_creates_items(self):
        client = self._create_superuser_client()
        url = reverse("admin:tracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        response = client.post(url)
        self.assertEqual(response.status_code, 302)

        new_fy = FinancialYear.objects.get(year=2025)
        new_items = list(new_fy.items.select_related("parent").order_by("order"))
        self.assertEqual(len(new_items), 2)

        root_copy = new_items[0]
        self.assertEqual(root_copy.title, "Root")
        self.assertEqual(root_copy.status, Item.STATUS_PENDING)
        self.assertEqual(root_copy.notes, "")

        child_copy = new_fy.items.get(parent=root_copy)
        self.assertEqual(child_copy.title, "Child")

    def test_copy_existing_year_shows_error(self):
        FinancialYear.objects.create(year=2025)
        client = self._create_superuser_client()
        url = reverse("admin:tracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        response = client.post(url)
        # Should redirect back with error message
        self.assertEqual(response.status_code, 302)
        self.assertFalse(FinancialYear.objects.filter(year=2026).exists())


class AdminViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            "admin", "admin@example.com", "password"
        )
        self.client = Client()
        self.client.login(username="admin", password="password")
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Test Item", order=1)

    def test_changelist(self):
        url = reverse("admin:tracker_financialyear_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_summary_view(self):
        url = reverse("admin:tracker_financialyear_summary", args=[self.fy.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FY2024")
        self.assertContains(response, "Test Item")

    def test_download_zip_view(self):
        url = reverse("admin:tracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        buf = io.BytesIO(
            b"".join(
                response.streaming_content
                if hasattr(response, "streaming_content")
                else [response.content]
            )
        )
        with zipfile.ZipFile(buf) as zf:
            self.assertIn("index.md", zf.namelist())

    def test_download_db_backup_view(self):
        url = reverse("admin:tracker_financialyear_download_db_backup")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-sqlite3")
        self.assertIn("db-backup.sqlite3", response["Content-Disposition"])
        # Content should be a valid SQLite database (starts with the magic header)
        self.assertTrue(response.content[:16].startswith(b"SQLite format 3\x00"))

    def test_download_db_backup_requires_superuser(self):
        non_super = User.objects.create_user("regular", "r@example.com", "pass")
        non_super.is_staff = True
        non_super.save()
        client = Client()
        client.login(username="regular", password="pass")
        url = reverse("admin:tracker_financialyear_download_db_backup")
        response = client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_copy_to_new_year_get(self):
        url = reverse("admin:tracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FY2025")


class EnsureSuperuserCommandTests(TestCase):
    """Tests for the ensure_superuser management command."""

    def _call(self, **kwargs):
        """Call ensure_superuser and return captured stdout."""
        from io import StringIO

        out = StringIO()
        call_command("ensure_superuser", stdout=out, **kwargs)
        return out.getvalue()

    def test_creates_new_user_with_random_password(self):
        output = self._call(username="testadmin", email="a@example.com")
        self.assertTrue(User.objects.filter(username="testadmin").exists())
        user = User.objects.get(username="testadmin")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_active)
        self.assertIn("testadmin", output)
        # Password is the last word on the first output line, 32 hex chars.
        first_line = output.splitlines()[0]
        printed_password = first_line.split()[-1]
        self.assertEqual(len(printed_password), 32)
        self.assertTrue(user.check_password(printed_password))

    def test_default_username_is_admin(self):
        output = self._call()
        self.assertTrue(User.objects.filter(username="admin").exists())
        self.assertIn("admin", output)

    def test_existing_user_no_change_when_already_superuser_with_password(self):
        User.objects.create_superuser("admin", "", "existingpassword")
        output = self._call(username="admin")
        # No password should be printed when user already has one
        self.assertEqual(output.strip(), "")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("existingpassword"))

    def test_existing_user_flags_repaired(self):
        user = User.objects.create_user("admin", "", "existingpassword")
        user.is_active = False
        user.is_staff = False
        user.is_superuser = False
        user.save()
        self._call(username="admin")
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        # Password unchanged — existing password should still work
        self.assertTrue(user.check_password("existingpassword"))

    def test_existing_user_no_password_gets_new_password(self):
        user = User.objects.create_superuser("admin", "", "tmp")
        user.set_unusable_password()
        user.save()
        output = self._call(username="admin")
        self.assertIn("admin", output)
        first_line = output.splitlines()[0]
        printed_password = first_line.split()[-1]
        self.assertEqual(len(printed_password), 32)
        user.refresh_from_db()
        self.assertTrue(user.check_password(printed_password))
