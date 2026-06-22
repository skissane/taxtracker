import base64
import datetime
import io
import json
import zipfile

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .admin import _adjust_notes_headings
from .archives import (
    UnsupportedArchiveError,
    extract_from_archive,
    extract_from_archive_with_skips,
)
from .forms import AttachmentForm, FlexibleDateField
from .models import (
    Attachment,
    DBStoredFile,
    FileExtension,
    FileType,
    FinancialYear,
    Item,
    MimeType,
    _extract_date_from_filename,
)


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
        self.assertEqual(str(self.parent), "FY2024 > Income")

    def test_str_child(self):
        self.assertEqual(str(self.child), "FY2024 > Income > Salary")

    def test_is_done_false(self):
        self.assertFalse(self.parent.is_done)

    def test_is_done_true(self):
        self.parent.status = Item.STATUS_DONE
        self.assertTrue(self.parent.is_done)

    def test_get_folder_path_root(self):
        self.assertEqual(self.parent.get_folder_path(), ["Income"])

    def test_get_folder_path_child(self):
        self.assertEqual(self.child.get_folder_path(), ["Income", "Salary"])


class ItemYearInheritanceTests(TestCase):
    """Model-level enforcement: child items always inherit parent's year."""

    def setUp(self):
        self.fy2024 = FinancialYear.objects.create(year=2024)
        self.fy2025 = FinancialYear.objects.create(year=2025)
        self.parent = Item.objects.create(year=self.fy2024, title="Root", order=1)

    def test_save_auto_corrects_child_year_mismatch(self):
        """save() silently corrects a mismatched year to match the parent."""
        child = Item(
            year=self.fy2025,  # wrong year
            parent=self.parent,
            title="Child",
            order=1,
        )
        child.save()
        child.refresh_from_db()
        self.assertEqual(child.year_id, self.fy2024.pk)

    def test_clean_auto_corrects_child_year_mismatch(self):
        """clean() silently corrects a mismatched year to match the parent."""
        child = Item(
            year=self.fy2025,  # wrong year
            parent=self.parent,
            title="Child",
            order=1,
        )
        child.clean()
        self.assertEqual(child.year_id, self.fy2024.pk)

    def test_save_with_correct_year_unchanged(self):
        """save() does not change year when child already has the correct year."""
        child = Item.objects.create(
            year=self.fy2024, parent=self.parent, title="Child", order=1
        )
        child.refresh_from_db()
        self.assertEqual(child.year_id, self.fy2024.pk)

    def test_root_item_year_not_affected(self):
        """Items without a parent keep whatever year is assigned."""
        root = Item.objects.create(year=self.fy2025, title="Root2", order=2)
        root.refresh_from_db()
        self.assertEqual(root.year_id, self.fy2025.pk)

    def test_direct_orm_save_corrects_year(self):
        """Direct ORM save (bypassing admin) still corrects the year."""
        child = Item.objects.create(
            year=self.fy2024, parent=self.parent, title="Child", order=1
        )
        # Directly change year via ORM (simulating a script/fixture misuse)
        child.year = self.fy2025
        child.save()
        child.refresh_from_db()
        self.assertEqual(child.year_id, self.fy2024.pk)


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

    def test_download_zip_spaces_replaced_with_underscores(self):
        """Spaces in item titles/filenames must become underscores in ZIP paths."""
        item = Item.objects.create(year=self.fy, title="My Item With Spaces", order=2)
        Attachment.objects.create(
            item=item,
            title="my doc",
            file=ContentFile(b"data", name="my doc.pdf"),
        )
        url = reverse("admin:tracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        buf = io.BytesIO(
            b"".join(
                response.streaming_content
                if hasattr(response, "streaming_content")
                else [response.content]
            )
        )
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            # No path component should contain a space.
            for name in names:
                self.assertNotIn(" ", name, f"Space found in ZIP path: {name!r}")
            # The item's folder should use underscores.
            self.assertTrue(
                any("My_Item_With_Spaces" in n for n in names),
                f"Expected underscore path not found in {names}",
            )
            # The attachment filename should also use underscores.
            self.assertTrue(
                any("my_doc.pdf" in n for n in names),
                f"Expected sanitized filename not found in {names}",
            )

    def test_download_zip_notes_only_item_in_index(self):
        """Items with notes but no attachments must still appear in index.md."""
        Item.objects.create(
            year=self.fy,
            title="Notes Only Item",
            notes="This is an important note.",
            order=2,
        )
        url = reverse("admin:tracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        buf = io.BytesIO(response.content)
        with zipfile.ZipFile(buf) as zf:
            index_content = zf.read("index.md").decode()
        self.assertIn("Notes_Only_Item", index_content)
        self.assertIn("This is an important note.", index_content)

    def test_download_zip_index_sections_sorted_by_folder_path(self):
        parent = Item.objects.create(year=self.fy, title="Brokerage accounts", order=2)
        child = Item.objects.create(
            year=self.fy,
            parent=parent,
            title="AAA Fidelity",
            order=1,
        )
        zeta = Item.objects.create(year=self.fy, title="Zeta top", order=1)
        Attachment.objects.create(
            item=parent,
            title="parent.pdf",
            file=ContentFile(b"parent", name="parent.pdf"),
        )
        Attachment.objects.create(
            item=child,
            title="child.pdf",
            file=ContentFile(b"child", name="child.pdf"),
        )
        Attachment.objects.create(
            item=zeta,
            title="zeta.pdf",
            file=ContentFile(b"zeta", name="zeta.pdf"),
        )

        url = reverse("admin:tracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        buf = io.BytesIO(response.content)
        with zipfile.ZipFile(buf) as zf:
            index_content = zf.read("index.md").decode()

        parent_heading = "## Brokerage_accounts\n"
        child_heading = "## Brokerage_accounts/AAA_Fidelity\n"
        zeta_heading = "## Zeta_top\n"
        self.assertLess(
            index_content.index(parent_heading), index_content.index(child_heading)
        )
        self.assertLess(
            index_content.index(child_heading), index_content.index(zeta_heading)
        )

    def test_download_multi_zip_get(self):
        """GET renders the multi-year ZIP selection form."""
        url = reverse("admin:tracker_financialyear_download_multi_zip")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FY2024")

    def test_download_multi_zip_post_returns_zip(self):
        """POST with action=zip returns a ZIP with a per-year top-level prefix."""
        fy2025 = FinancialYear.objects.create(year=2025)
        item2025 = Item.objects.create(year=fy2025, title="Item 2025", order=1)
        Attachment.objects.create(
            item=item2025,
            title="doc2025.pdf",
            file=ContentFile(b"data", name="doc2025.pdf"),
        )
        url = reverse("admin:tracker_financialyear_download_multi_zip")
        response = self.client.post(
            url,
            {"fy_ids": [self.fy.pk, fy2025.pk], "action": "zip"},
        )
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
            names = zf.namelist()
            # Each FY should have its own top-level prefix.
            self.assertTrue(
                any(n.startswith("FY2024/") for n in names),
                f"FY2024/ prefix missing in {names}",
            )
            self.assertTrue(
                any(n.startswith("FY2025/") for n in names),
                f"FY2025/ prefix missing in {names}",
            )

    def test_download_multi_zip_post_view_index(self):
        """POST with action=view_index returns markdown containing FY headers."""
        fy2025 = FinancialYear.objects.create(year=2025)
        url = reverse("admin:tracker_financialyear_download_multi_zip")
        response = self.client.post(
            url,
            {"fy_ids": [self.fy.pk, fy2025.pk], "action": "view_index"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/markdown", response["Content-Type"])
        content = response.content.decode()
        self.assertIn("FY2024", content)
        self.assertIn("FY2025", content)

    def test_download_db_backup_view(self):
        url = reverse("admin:tracker_financialyear_download_db_backup")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-sqlite3")
        self.assertRegex(
            response["Content-Disposition"],
            r"db-backup\.\d{4}-\d{2}-\d{2}\.\d{2}\.\d{2}\.\d{2}\.sqlite3",
        )
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

    def test_reassign_attachments_view_lists_movable_and_unmovable(self):
        fy2025 = FinancialYear.objects.create(year=2025)
        Item.objects.create(year=fy2025, title=self.item.title, order=self.item.order)

        Attachment.objects.create(
            item=self.item,
            title="in-year.pdf",
            # FY2024 spans 1 Jul 2023 - 30 Jun 2024.
            date=datetime.date(2024, 1, 1),
            file=ContentFile(b"in-year", name="in-year.pdf"),
        )
        Attachment.objects.create(
            item=self.item,
            title="undated.pdf",
            date=None,
            file=ContentFile(b"undated", name="undated.pdf"),
        )
        movable = Attachment.objects.create(
            item=self.item,
            title="movable.pdf",
            # Aug 2024 belongs to FY2025, so this can be reassigned.
            date=datetime.date(2024, 8, 1),
            file=ContentFile(b"movable", name="movable.pdf"),
        )
        Attachment.objects.create(
            item=self.item,
            title="no-target-fy.pdf",
            date=datetime.date(2025, 8, 1),
            file=ContentFile(b"no-target-fy", name="no-target-fy.pdf"),
        )

        url = reverse("admin:tracker_item_reassign_attachments", args=[self.item.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Total attachments: 4")
        self.assertContains(response, "Attachments on correct item: 2")
        self.assertContains(response, "Attachments unmovable: 1")
        self.assertContains(response, "Attachments movable: 1")
        self.assertContains(
            response,
            f'value="{movable.pk}"',
            html=False,
        )
        self.assertContains(response, "Financial year FY2026 not found.")

    def test_item_change_view_links_to_equivalent_prev_and_next_items(self):
        prev_fy = FinancialYear.objects.create(year=2023)
        next_fy = FinancialYear.objects.create(year=2025)

        prev_root = Item.objects.create(year=prev_fy, title="Parent", order=1)
        prev_item = Item.objects.create(
            year=prev_fy,
            parent=prev_root,
            title="Test Item",
            order=1,
        )

        current_root = Item.objects.create(year=self.fy, title="Parent", order=1)
        current_item = Item.objects.create(
            year=self.fy,
            parent=current_root,
            title="Test Item",
            order=1,
        )

        next_root = Item.objects.create(year=next_fy, title="Parent", order=1)
        next_item = Item.objects.create(
            year=next_fy,
            parent=next_root,
            title="Test Item",
            order=1,
        )

        url = reverse("admin:tracker_item_change", args=[current_item.pk])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("admin:tracker_item_change", args=[prev_item.pk]),
        )
        self.assertContains(response, "← Previous Year (FY2023)")
        self.assertContains(
            response,
            reverse("admin:tracker_item_change", args=[next_item.pk]),
        )
        self.assertContains(response, "Next Year (FY2025) →")

    def test_item_change_view_missing_object_uses_admin_redirect(self):
        url = reverse("admin:tracker_item_change", args=[999999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_reassign_attachments_view_moves_selected(self):
        fy2025 = FinancialYear.objects.create(year=2025)
        fy2023 = FinancialYear.objects.create(year=2023)
        target_2025 = Item.objects.create(
            year=fy2025, title=self.item.title, order=self.item.order
        )
        Item.objects.create(year=fy2023, title=self.item.title, order=self.item.order)

        to_move = Attachment.objects.create(
            item=self.item,
            title="to-move.pdf",
            date=datetime.date(2024, 8, 2),
            file=ContentFile(b"to-move", name="to-move.pdf"),
        )
        not_selected = Attachment.objects.create(
            item=self.item,
            title="not-selected.pdf",
            date=datetime.date(2023, 1, 2),
            file=ContentFile(b"not-selected", name="not-selected.pdf"),
        )
        unmovable = Attachment.objects.create(
            item=self.item,
            title="unmovable.pdf",
            date=datetime.date(2025, 8, 2),
            file=ContentFile(b"unmovable", name="unmovable.pdf"),
        )

        url = reverse("admin:tracker_item_reassign_attachments", args=[self.item.pk])
        response = self.client.post(
            url,
            data={"move_attachment_ids": [str(to_move.pk)]},
        )
        self.assertEqual(response.status_code, 200)

        to_move.refresh_from_db()
        not_selected.refresh_from_db()
        unmovable.refresh_from_db()
        self.assertEqual(to_move.item_id, target_2025.pk)
        self.assertEqual(not_selected.item_id, self.item.pk)
        self.assertEqual(unmovable.item_id, self.item.pk)

        self.assertContains(response, "Moved successfully: 1")
        self.assertContains(response, "Not selected to move: 1")
        self.assertContains(response, "Unmovable: 1")
        self.assertContains(response, "FY2025")
        self.assertContains(response, "FY2023")


class ItemChildInlineTests(TestCase):
    """Tests for child-item year-inheritance fixes in ItemAdmin."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "admin@example.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")
        self.fy = FinancialYear.objects.create(year=2024)
        self.parent_item = Item.objects.create(year=self.fy, title="Parent", order=1)

    def _item_change_url(self, item):
        return reverse("admin:tracker_item_change", args=[item.pk])

    def test_inline_child_inherits_year_on_save(self):
        """Saving a new inline sub-item must not raise IntegrityError."""
        url = self._item_change_url(self.parent_item)
        data = {
            "year": self.fy.pk,
            "parent": "",
            "order": "1",
            "title": "Parent",
            "status": "pending",
            "notes": "",
            # Management form for ChildItemInline (prefix = "children")
            "children-TOTAL_FORMS": "1",
            "children-INITIAL_FORMS": "0",
            "children-MIN_NUM_FORMS": "0",
            "children-MAX_NUM_FORMS": "1000",
            "children-0-order": "0",
            "children-0-title": "Child Item",
            "children-0-status": "pending",
            "children-0-notes": "",
            "children-0-id": "",
            # Management form for AttachmentInline
            "attachments-TOTAL_FORMS": "0",
            "attachments-INITIAL_FORMS": "0",
            "attachments-MIN_NUM_FORMS": "0",
            "attachments-MAX_NUM_FORMS": "1000",
        }
        response = self.client.post(url, data)
        # Should redirect on success (not 200 = form error, not 500 = IntegrityError)
        self.assertEqual(response.status_code, 302)
        child = Item.objects.get(title="Child Item")
        self.assertEqual(child.year_id, self.fy.pk)
        self.assertEqual(child.parent_id, self.parent_item.pk)

    def test_year_field_hidden_for_child_item(self):
        """Year field should not appear in the change form for child items."""
        child = Item.objects.create(
            year=self.fy, parent=self.parent_item, title="Child", order=1
        )
        url = self._item_change_url(child)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # The year select widget should NOT be rendered for child items.
        self.assertNotContains(response, 'id="id_year"')

    def test_year_field_present_for_root_item(self):
        """Year field must appear in the change form for root items."""
        url = self._item_change_url(self.parent_item)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="id_year"')

    def test_cascade_year_on_root_item_save(self):
        """Changing a root item's year must cascade to all descendants."""
        fy2025 = FinancialYear.objects.create(year=2025)
        child = Item.objects.create(
            year=self.fy, parent=self.parent_item, title="Child", order=1
        )
        grandchild = Item.objects.create(
            year=self.fy, parent=child, title="Grandchild", order=1
        )

        url = self._item_change_url(self.parent_item)
        data = {
            "year": fy2025.pk,
            "parent": "",
            "order": "1",
            "title": "Parent",
            "status": "pending",
            "notes": "",
            "children-TOTAL_FORMS": "1",
            "children-INITIAL_FORMS": "1",
            "children-MIN_NUM_FORMS": "0",
            "children-MAX_NUM_FORMS": "1000",
            "children-0-id": str(child.pk),
            "children-0-parent": str(self.parent_item.pk),
            "children-0-order": "1",
            "children-0-title": "Child",
            "children-0-status": "pending",
            "children-0-notes": "",
            "attachments-TOTAL_FORMS": "0",
            "attachments-INITIAL_FORMS": "0",
            "attachments-MIN_NUM_FORMS": "0",
            "attachments-MAX_NUM_FORMS": "1000",
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)

        child.refresh_from_db()
        grandchild.refresh_from_db()
        self.assertEqual(child.year_id, fy2025.pk)
        self.assertEqual(grandchild.year_id, fy2025.pk)


class ItemParentCycleTests(TestCase):
    """Tests that circular parent references are rejected with a ValidationError."""

    def setUp(self):
        self.fy = FinancialYear.objects.create(year=2024)
        self.user = User.objects.create_superuser("admin", "admin@example.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")

    def test_direct_cycle_raises_validation_error(self):
        """Item cannot be its own parent (A → A)."""
        item = Item.objects.create(year=self.fy, title="A", order=1)
        item.parent_id = item.pk
        with self.assertRaises(ValidationError):
            item.clean()

    def test_indirect_two_element_cycle_raises_validation_error(self):
        """Two-element cycle: A → B, then B → A should be rejected."""
        b = Item.objects.create(year=self.fy, title="B", order=2)
        a = Item.objects.create(year=self.fy, parent=b, title="A", order=1)
        b.parent_id = a.pk
        with self.assertRaises(ValidationError):
            b.clean()

    def test_indirect_three_element_cycle_raises_validation_error(self):
        """Three-element cycle: A → B → C, then C → A should be rejected."""
        c = Item.objects.create(year=self.fy, title="C", order=3)
        b = Item.objects.create(year=self.fy, parent=c, title="B", order=2)
        a = Item.objects.create(year=self.fy, parent=b, title="A", order=1)
        c.parent_id = a.pk
        with self.assertRaises(ValidationError):
            c.clean()

    def _item_change_url(self, item):
        return reverse("admin:tracker_item_change", args=[item.pk])

    def test_admin_form_rejects_direct_cycle(self):
        """Admin form shows a form error (not RecursionError) for self-parent."""
        item = Item.objects.create(year=self.fy, title="A", order=1)
        url = self._item_change_url(item)
        data = {
            "year": self.fy.pk,
            "parent": str(item.pk),  # self-reference
            "order": "1",
            "title": "A",
            "status": "pending",
            "notes": "",
            "children-TOTAL_FORMS": "0",
            "children-INITIAL_FORMS": "0",
            "children-MIN_NUM_FORMS": "0",
            "children-MAX_NUM_FORMS": "1000",
            "attachments-TOTAL_FORMS": "0",
            "attachments-INITIAL_FORMS": "0",
            "attachments-MIN_NUM_FORMS": "0",
            "attachments-MAX_NUM_FORMS": "1000",
        }
        response = self.client.post(url, data)
        # Should show the form with errors (200), not redirect (302) or crash (500).
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertIsNone(item.parent_id)

    def test_admin_form_rejects_indirect_three_element_cycle(self):
        """Admin form rejects A → B → C → A cycle."""
        c = Item.objects.create(year=self.fy, title="C", order=3)
        b = Item.objects.create(year=self.fy, parent=c, title="B", order=2)
        a = Item.objects.create(year=self.fy, parent=b, title="A", order=1)
        # Try to make C's parent = A, closing the cycle.
        url = self._item_change_url(c)
        data = {
            "year": self.fy.pk,
            "parent": str(a.pk),
            "order": "3",
            "title": "C",
            "status": "pending",
            "notes": "",
            "children-TOTAL_FORMS": "0",
            "children-INITIAL_FORMS": "0",
            "children-MIN_NUM_FORMS": "0",
            "children-MAX_NUM_FORMS": "1000",
            "attachments-TOTAL_FORMS": "0",
            "attachments-INITIAL_FORMS": "0",
            "attachments-MIN_NUM_FORMS": "0",
            "attachments-MAX_NUM_FORMS": "1000",
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        c.refresh_from_db()
        self.assertIsNone(c.parent_id)

    def test_str_is_cycle_safe_for_existing_cycle_in_db(self):
        """Item.__str__ must not raise RecursionError if a cycle exists in DB."""
        # Bypass clean() to force a cycle directly into the DB.
        a = Item.objects.create(year=self.fy, title="A", order=1)
        b = Item.objects.create(year=self.fy, title="B", order=2)
        # Create cycle: A → B → A via raw update to bypass validation.
        Item.objects.filter(pk=a.pk).update(parent_id=b.pk)
        Item.objects.filter(pk=b.pk).update(parent_id=a.pk)
        a.refresh_from_db()
        b.refresh_from_db()
        # __str__ must not raise a RecursionError; cycle is indicated by "…".
        try:
            result = str(a)
        except RecursionError:
            self.fail("Item.__str__ raised RecursionError on a cyclic item")
        self.assertIn("A", result)
        self.assertIn("…", result)
        try:
            result_b = str(b)
        except RecursionError:
            self.fail("Item.__str__ raised RecursionError on a cyclic item")
        self.assertIn("B", result_b)
        self.assertIn("…", result_b)

    def test_existing_cycle_does_not_block_resave(self):
        """clean() must not raise ValidationError when parent_id is unchanged.

        An item already in a cycle (forced into DB before cycle prevention was
        added) appears in its own inline Sub-items formset.  When the admin
        re-validates that unchanged inline row it must not surface an invisible
        "Please correct the error below" message.
        """
        item = Item.objects.create(year=self.fy, title="Madness", order=1)
        # Force a self-cycle directly in the DB, bypassing clean().
        Item.objects.filter(pk=item.pk).update(parent_id=item.pk)
        item.refresh_from_db()
        # clean() must be a no-op when parent_id hasn't changed.
        try:
            item.clean()
        except ValidationError:
            self.fail(
                "clean() raised ValidationError for an unchanged pre-existing cycle"
            )

    def test_admin_can_break_cycle_by_setting_parent_to_none(self):
        """Admin save succeeds when a cyclic item's parent is changed to None."""
        item = Item.objects.create(year=self.fy, title="Madness", order=1)
        # Force a self-cycle directly in the DB.
        Item.objects.filter(pk=item.pk).update(parent_id=item.pk)
        item.refresh_from_db()
        url = self._item_change_url(item)
        data = {
            "year": self.fy.pk,
            "parent": "",  # clearing the parent to break the cycle
            "order": "1",
            "title": "Madness",
            "status": "pending",
            "notes": "",
            "children-TOTAL_FORMS": "1",
            "children-INITIAL_FORMS": "1",
            "children-MIN_NUM_FORMS": "0",
            "children-MAX_NUM_FORMS": "1000",
            # The cyclic item appears as its own child in the inline.
            "children-0-id": str(item.pk),
            "children-0-parent": str(item.pk),
            "children-0-order": "1",
            "children-0-title": "Madness",
            "children-0-status": "pending",
            "children-0-notes": "",
            "attachments-TOTAL_FORMS": "0",
            "attachments-INITIAL_FORMS": "0",
            "attachments-MIN_NUM_FORMS": "0",
            "attachments-MAX_NUM_FORMS": "1000",
        }
        response = self.client.post(url, data)
        # Must redirect (302) on success — a 200 means the form was re-rendered
        # with errors, and a 500 means an unhandled exception.
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertIsNone(item.parent_id)


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


class FileTypeModelTests(TestCase):
    """Tests for FileType, MimeType, and FileExtension models."""

    def setUp(self):
        self.ft = FileType.objects.create(short_name="PDF", full_name="PDF Document")

    def test_filetype_str(self):
        self.assertEqual(str(self.ft), "PDF")

    def test_filetype_short_name_unique(self):
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            FileType.objects.create(short_name="PDF", full_name="Other PDF")

    def test_filetype_full_name_unique(self):
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            FileType.objects.create(short_name="PDF2", full_name="PDF Document")

    # ------------------------------------------------------------------
    # MimeType tests
    # ------------------------------------------------------------------

    def test_mimetype_str(self):
        mt = MimeType.objects.create(
            file_type=self.ft, mime_type="application/pdf", is_primary=True
        )
        self.assertEqual(str(mt), "application/pdf")

    def test_mimetype_valid_formats(self):
        valid = [
            "application/pdf",
            "image/jpeg",
            "text/plain",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/x-pdf",
        ]
        for mt_str in valid:
            mt = MimeType(file_type=self.ft, mime_type=mt_str)
            mt.clean()  # should not raise

    def test_mimetype_invalid_no_slash(self):
        mt = MimeType(file_type=self.ft, mime_type="applicationpdf")
        with self.assertRaises(ValidationError):
            mt.clean()

    def test_mimetype_invalid_two_slashes(self):
        mt = MimeType(file_type=self.ft, mime_type="application/pdf/extra")
        with self.assertRaises(ValidationError):
            mt.clean()

    def test_mimetype_invalid_with_space(self):
        mt = MimeType(file_type=self.ft, mime_type="application /pdf")
        with self.assertRaises(ValidationError):
            mt.clean()

    def test_mimetype_globally_unique(self):
        from django.db import IntegrityError

        ft2 = FileType.objects.create(short_name="PDF2", full_name="PDF 2")
        MimeType.objects.create(
            file_type=self.ft, mime_type="application/pdf", is_primary=True
        )
        with self.assertRaises(IntegrityError):
            MimeType.objects.create(
                file_type=ft2, mime_type="application/pdf", is_primary=False
            )

    def test_at_most_one_primary_mime_type_per_file_type(self):
        from django.db import IntegrityError

        MimeType.objects.create(
            file_type=self.ft, mime_type="application/pdf", is_primary=True
        )
        with self.assertRaises(IntegrityError):
            MimeType.objects.create(
                file_type=self.ft, mime_type="application/x-pdf", is_primary=True
            )

    # ------------------------------------------------------------------
    # FileExtension tests
    # ------------------------------------------------------------------

    def test_extension_str(self):
        ext = FileExtension.objects.create(
            file_type=self.ft, extension="pdf", is_primary=True
        )
        self.assertEqual(str(ext), "pdf")

    def test_extension_forced_lowercase_on_save(self):
        ext = FileExtension(file_type=self.ft, extension="PDF", is_primary=True)
        ext.save()
        ext.refresh_from_db()
        self.assertEqual(ext.extension, "pdf")

    def test_extension_forced_lowercase_in_clean(self):
        ext = FileExtension(file_type=self.ft, extension="PDF")
        ext.clean()
        self.assertEqual(ext.extension, "pdf")

    def test_extension_invalid_with_dot(self):
        ext = FileExtension(file_type=self.ft, extension=".pdf")
        with self.assertRaises(ValidationError):
            ext.clean()

    def test_extension_invalid_with_slash(self):
        ext = FileExtension(file_type=self.ft, extension="pd/f")
        with self.assertRaises(ValidationError):
            ext.clean()

    def test_extension_invalid_with_space(self):
        ext = FileExtension(file_type=self.ft, extension="p df")
        with self.assertRaises(ValidationError):
            ext.clean()

    def test_extension_globally_unique(self):
        from django.db import IntegrityError

        ft2 = FileType.objects.create(short_name="PDF2", full_name="PDF 2")
        FileExtension.objects.create(
            file_type=self.ft, extension="pdf", is_primary=True
        )
        with self.assertRaises(IntegrityError):
            FileExtension.objects.create(
                file_type=ft2, extension="pdf", is_primary=False
            )

    def test_at_most_one_primary_extension_per_file_type(self):
        from django.db import IntegrityError

        FileExtension.objects.create(
            file_type=self.ft, extension="pdf", is_primary=True
        )
        with self.assertRaises(IntegrityError):
            FileExtension.objects.create(
                file_type=self.ft, extension="pdff", is_primary=True
            )


class AttachmentTitleTests(TestCase):
    """Tests for Attachment.title auto-population and FK file_type."""

    def setUp(self):
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Income", order=1)
        self.ft = FileType.objects.create(short_name="PDF", full_name="PDF Document")

    def _make_simple_file(self, name="document.pdf"):
        from django.core.files.base import ContentFile

        return ContentFile(b"%PDF-1.4 test", name=name)

    def test_title_auto_populated_from_filename(self):
        att = Attachment(item=self.item, file=self._make_simple_file("report.pdf"))
        att.save()
        self.assertEqual(att.title, "report.pdf")

    def test_explicit_title_not_overwritten(self):
        att = Attachment(
            item=self.item,
            title="My Report",
            file=self._make_simple_file("report.pdf"),
        )
        att.save()
        self.assertEqual(att.title, "My Report")

    def test_filetype_fk_saves_and_loads(self):
        att = Attachment(
            item=self.item,
            title="My Report",
            file_type=self.ft,
            file=self._make_simple_file("report.pdf"),
        )
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.file_type, self.ft)

    def test_filetype_nullable(self):
        att = Attachment(
            item=self.item,
            title="No Type",
            file=self._make_simple_file("report.pdf"),
        )
        att.save()
        att.refresh_from_db()
        self.assertIsNone(att.file_type)


class AttachmentDateTests(TestCase):
    """Tests for Attachment.date auto-extraction from filename."""

    def setUp(self):
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Income", order=1)

    def _make_file(self, name):
        return ContentFile(b"data", name=name)

    # ------------------------------------------------------------------
    # Unit tests for the helper function
    # ------------------------------------------------------------------

    def test_extract_iso_date(self):
        self.assertEqual(
            _extract_date_from_filename("statement-2026-01-15.pdf"),
            datetime.date(2026, 1, 15),
        )

    def test_extract_compact_date_no_separator(self):
        self.assertEqual(
            _extract_date_from_filename("report20260115.pdf"),
            datetime.date(2026, 1, 15),
        )

    def test_extract_date_followed_by_digits(self):
        """DD may be followed by digits – the match is still valid."""
        self.assertEqual(
            _extract_date_from_filename("20260101123.pdf"),
            datetime.date(2026, 1, 1),
        )

    def test_year_preceded_by_non_digit_allowed(self):
        """Year preceded by a non-digit character (e.g. '-') is valid."""
        self.assertEqual(
            _extract_date_from_filename("123-2026-01-01.pdf"),
            datetime.date(2026, 1, 1),
        )

    def test_year_preceded_by_digit_rejected(self):
        """Year directly preceded by another digit must not be matched."""
        self.assertIsNone(_extract_date_from_filename("1232026-01-01.pdf"))

    def test_invalid_calendar_date_skipped(self):
        """A pattern that is not a real date (e.g. month 13) must be ignored."""
        self.assertIsNone(_extract_date_from_filename("2026-13-45.pdf"))

    def test_returns_first_valid_date(self):
        """When multiple date patterns exist, the first valid one is returned."""
        self.assertEqual(
            _extract_date_from_filename("2026-13-45_2025-06-30.txt"),
            datetime.date(2025, 6, 30),
        )

    def test_no_date_in_filename(self):
        self.assertIsNone(_extract_date_from_filename("nodatehere.pdf"))

    # ------------------------------------------------------------------
    # Integration tests via Attachment.save()
    # ------------------------------------------------------------------

    def test_date_auto_extracted_from_filename(self):
        att = Attachment(
            item=self.item, file=self._make_file("statement-2026-03-15.pdf")
        )
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.date, datetime.date(2026, 3, 15))

    def test_explicit_date_not_overwritten(self):
        explicit = datetime.date(2000, 1, 1)
        att = Attachment(
            item=self.item,
            date=explicit,
            file=self._make_file("statement-2026-03-15.pdf"),
        )
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.date, explicit)

    def test_date_none_when_not_in_filename(self):
        att = Attachment(item=self.item, file=self._make_file("nodatehere.pdf"))
        att.save()
        att.refresh_from_db()
        self.assertIsNone(att.date)

    def test_date_none_when_not_supplied_and_not_extractable(self):
        att = Attachment(item=self.item, file=self._make_file("report.pdf"))
        att.save()
        att.refresh_from_db()
        self.assertIsNone(att.date)


class AttachmentDateInputFormatTests(TestCase):
    """Tests for FlexibleDateField and the extra English date-input formats."""

    def setUp(self):
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Income", order=1)
        self.field = FlexibleDateField(required=False)

    # ------------------------------------------------------------------
    # Unit tests for FlexibleDateField
    # ------------------------------------------------------------------

    def test_iso_format_accepted(self):
        self.assertEqual(self.field.clean("2023-07-20"), datetime.date(2023, 7, 20))

    def test_day_month_year_full_name(self):
        """'20 July 2023' should parse to 2023-07-20."""
        self.assertEqual(self.field.clean("20 July 2023"), datetime.date(2023, 7, 20))

    def test_month_day_comma_year_full_name(self):
        """'June 30, 2023' should parse to 2023-06-30."""
        self.assertEqual(self.field.clean("June 30, 2023"), datetime.date(2023, 6, 30))

    def test_day_month_year_abbreviated(self):
        """'20 Jul 2023' should parse to 2023-07-20."""
        self.assertEqual(self.field.clean("20 Jul 2023"), datetime.date(2023, 7, 20))

    def test_month_day_comma_year_abbreviated(self):
        """'Jun 30, 2023' should parse to 2023-06-30."""
        self.assertEqual(self.field.clean("Jun 30, 2023"), datetime.date(2023, 6, 30))

    def test_invalid_date_string_raises(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            self.field.clean("not a date")

    def test_empty_string_returns_none(self):
        self.assertIsNone(self.field.clean(""))

    # ------------------------------------------------------------------
    # Integration: AttachmentForm accepts the extra formats
    # ------------------------------------------------------------------

    def _form_data(self, date_str):
        return {
            "item": self.item.pk,
            "title": "Test",
            "date": date_str,
            "notes": "",
        }

    def test_form_accepts_day_full_month_year(self):
        """'20 July 2023' must not produce a date validation error."""
        form = AttachmentForm(data=self._form_data("20 July 2023"))
        self.assertNotIn("date", form.errors)

    def test_form_accepts_full_month_day_comma_year(self):
        """'June 30, 2023' must not produce a date validation error."""
        form = AttachmentForm(data=self._form_data("June 30, 2023"))
        self.assertNotIn("date", form.errors)

    def test_form_accepts_iso_date(self):
        """ISO format '2023-07-20' must not produce a date validation error."""
        form = AttachmentForm(data=self._form_data("2023-07-20"))
        self.assertNotIn("date", form.errors)

    def test_form_rejects_invalid_date(self):
        form = AttachmentForm(data=self._form_data("not a date"))
        self.assertIn("date", form.errors)


class DatabaseStorageTests(TestCase):
    """Tests for DatabaseStorage backend and file-storage related behaviours."""

    def setUp(self):
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Income", order=1)
        self.ft = FileType.objects.create(short_name="PDF", full_name="PDF Document")
        FileExtension.objects.create(
            file_type=self.ft, extension="pdf", is_primary=True
        )

    def _simple_file(self, name="document.pdf", content=b"%PDF-1.4 test"):
        return ContentFile(content, name=name)

    # ------------------------------------------------------------------
    # File stored in database (not on disk)
    # ------------------------------------------------------------------

    def test_attachment_file_stored_in_db(self):
        """Uploading a file should create a DBStoredFile record."""
        before = DBStoredFile.objects.count()
        att = Attachment(item=self.item, file=self._simple_file("doc.pdf"))
        att.save()
        self.assertEqual(DBStoredFile.objects.count(), before + 1)

    def test_attachment_file_content_retrievable(self):
        """The file content saved to DB should match what was uploaded."""
        data = b"Hello database storage"
        att = Attachment(item=self.item, file=self._simple_file("hello.txt", data))
        att.save()
        att.refresh_from_db()
        stored_content = att.file.read()
        self.assertEqual(stored_content, data)

    def test_attachment_file_name_stored_correctly(self):
        """DBStoredFile.filename should be the uploaded filename."""
        att = Attachment(item=self.item, file=self._simple_file("myfile.pdf"))
        att.save()
        db_file = DBStoredFile.objects.get(
            pk=att.file.storage._name_to_pk(att.file.name)
        )
        self.assertEqual(db_file.filename, "myfile.pdf")

    def test_attachment_file_deleted_removes_db_record(self):
        """Deleting an Attachment should remove the DBStoredFile record."""
        att = Attachment(item=self.item, file=self._simple_file("del.pdf"))
        att.save()
        pk = att.file.storage._name_to_pk(att.file.name)
        att.delete()
        self.assertFalse(DBStoredFile.objects.filter(pk=pk).exists())

    # ------------------------------------------------------------------
    # Auto-guess file type from extension
    # ------------------------------------------------------------------

    def test_auto_guess_file_type_from_extension(self):
        """file_type should be auto-set from the file extension when not provided."""
        att = Attachment(item=self.item, file=self._simple_file("report.pdf"))
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.file_type, self.ft)

    def test_auto_guess_file_type_case_insensitive(self):
        """Extension matching should be case-insensitive (e.g. .PDF → pdf)."""
        att = Attachment(item=self.item, file=self._simple_file("report.PDF"))
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.file_type, self.ft)

    def test_auto_guess_file_type_unknown_extension_leaves_null(self):
        """An unrecognised extension should leave file_type as None."""
        att = Attachment(item=self.item, file=self._simple_file("report.xyz"))
        att.save()
        att.refresh_from_db()
        self.assertIsNone(att.file_type)

    def test_explicit_file_type_not_overwritten(self):
        """An explicitly set file_type should not be overwritten by auto-guess."""
        ft2 = FileType.objects.create(short_name="TXT", full_name="Text File")
        att = Attachment(
            item=self.item, file=self._simple_file("report.pdf"), file_type=ft2
        )
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.file_type, ft2)

    # ------------------------------------------------------------------
    # File serving view
    # ------------------------------------------------------------------

    def test_serve_file_view(self):
        """The serve_file_view should return the correct file content."""
        User.objects.create_superuser("admin", "a@b.com", "pass")
        client = Client()
        client.login(username="admin", password="pass")

        content = b"Hello attachment content"
        att = Attachment(item=self.item, file=self._simple_file("served.txt", content))
        att.save()
        pk = att.file.storage._name_to_pk(att.file.name)

        url = reverse("admin:tracker_attachment_serve_file", args=[pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, content)
        self.assertIn("served.txt", response.get("Content-Disposition", ""))

    def test_serve_file_view_requires_login(self):
        """Unauthenticated requests to the serve view should redirect to login."""
        att = Attachment(item=self.item, file=self._simple_file("secret.pdf"))
        att.save()
        pk = att.file.storage._name_to_pk(att.file.name)
        url = reverse("admin:tracker_attachment_serve_file", args=[pk])
        response = Client().get(url)
        # Django admin redirects unauthenticated users to the login page
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])


class FileTypePrimaryValidationTests(TestCase):
    """Tests for the 'at least one primary' admin formset validation."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")

    def _post_filetype(self, mime_types=None, extensions=None):
        """POST to FileType add view with inline mime types and extensions."""
        data = {
            "short_name": "TST",
            "full_name": "Test Type",
            # Management forms
            "mime_types-TOTAL_FORMS": str(len(mime_types or [])),
            "mime_types-INITIAL_FORMS": "0",
            "mime_types-MIN_NUM_FORMS": "0",
            "mime_types-MAX_NUM_FORMS": "1000",
            "file_extensions-TOTAL_FORMS": str(len(extensions or [])),
            "file_extensions-INITIAL_FORMS": "0",
            "file_extensions-MIN_NUM_FORMS": "0",
            "file_extensions-MAX_NUM_FORMS": "1000",
        }
        for i, mt in enumerate(mime_types or []):
            data[f"mime_types-{i}-mime_type"] = mt["mime_type"]
            data[f"mime_types-{i}-is_primary"] = "on" if mt.get("is_primary") else ""
            data[f"mime_types-{i}-id"] = ""
            data[f"mime_types-{i}-file_type"] = ""
        for i, ext in enumerate(extensions or []):
            data[f"file_extensions-{i}-extension"] = ext["extension"]
            data[f"file_extensions-{i}-is_primary"] = (
                "on" if ext.get("is_primary") else ""
            )
            data[f"file_extensions-{i}-id"] = ""
            data[f"file_extensions-{i}-file_type"] = ""
        url = reverse("admin:tracker_filetype_add")
        return self.client.post(url, data)

    # ------------------------------------------------------------------
    # Auto-set primary (single row, not marked primary)
    # ------------------------------------------------------------------

    def test_single_mime_type_without_primary_auto_sets_primary(self):
        """Exactly one MIME type with is_primary unchecked should be auto-set."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": False}],
            extensions=[{"extension": "tst", "is_primary": True}],
        )
        self.assertEqual(response.status_code, 302)
        ft = FileType.objects.get(short_name="TST")
        self.assertTrue(ft.mime_types.get().is_primary)

    def test_single_extension_without_primary_auto_sets_primary(self):
        """Exactly one extension with is_primary unchecked should be auto-set."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}],
            extensions=[{"extension": "tst", "is_primary": False}],
        )
        self.assertEqual(response.status_code, 302)
        ft = FileType.objects.get(short_name="TST")
        self.assertTrue(ft.file_extensions.get().is_primary)

    # ------------------------------------------------------------------
    # Multiple rows — primary required, or error
    # ------------------------------------------------------------------

    def test_multiple_mime_types_none_primary_fails(self):
        """Multiple MIME types with none marked primary must fail validation."""
        response = self._post_filetype(
            mime_types=[
                {"mime_type": "application/tst", "is_primary": False},
                {"mime_type": "application/x-tst", "is_primary": False},
            ]
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "At least one MIME type must be marked as primary"
        )
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    def test_multiple_extensions_none_primary_fails(self):
        """Multiple extensions with none marked primary must fail validation."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}],
            extensions=[
                {"extension": "tst", "is_primary": False},
                {"extension": "ts2", "is_primary": False},
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "At least one file extension must be marked as primary"
        )
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    # ------------------------------------------------------------------
    # Multiple primaries — validation error instead of IntegrityError
    # ------------------------------------------------------------------

    def test_multiple_primary_mime_types_shows_validation_error(self):
        """Multiple MIME types all marked primary must produce a validation error."""
        response = self._post_filetype(
            mime_types=[
                {"mime_type": "application/tst", "is_primary": True},
                {"mime_type": "application/x-tst", "is_primary": True},
            ]
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only one MIME type may be marked as primary")
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    def test_multiple_primary_extensions_shows_validation_error(self):
        """Multiple extensions all marked primary must produce a validation error."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}],
            extensions=[
                {"extension": "tst", "is_primary": True},
                {"extension": "ts2", "is_primary": True},
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Only one file extension may be marked as primary"
        )
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    # ------------------------------------------------------------------
    # Successful saves
    # ------------------------------------------------------------------

    def test_with_primary_set_saves_successfully(self):
        """FileType with at least one primary mime type and extension should save."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}],
            extensions=[{"extension": "tst", "is_primary": True}],
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(FileType.objects.filter(short_name="TST").exists())

    def test_only_extensions_no_mime_types_fails(self):
        """FileType with only file extensions (no MIME types) must fail validation."""
        response = self._post_filetype(
            extensions=[{"extension": "tst", "is_primary": True}]
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A file type must have at least one MIME type")
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    def test_only_mime_types_no_extensions_fails(self):
        """FileType with only MIME types (no file extensions) must fail validation."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}]
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "A file type must have at least one file extension"
        )
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    # ------------------------------------------------------------------
    # At-least-one independent requirement
    # ------------------------------------------------------------------

    def test_no_mime_or_extension_fails_validation(self):
        """Saving a FileType with no MIME types and no extensions must fail."""
        response = self._post_filetype()
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "A file type must have at least one MIME type",
        )
        self.assertContains(
            response,
            "A file type must have at least one file extension",
        )
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())


# ---------------------------------------------------------------------------
# _adjust_notes_headings unit tests
# ---------------------------------------------------------------------------


class AdjustNotesHeadingsTests(TestCase):
    def test_no_headings_unchanged(self):
        notes = "Just some plain text.\nNo headings here."
        self.assertEqual(_adjust_notes_headings(notes, 2), notes)

    def test_single_h1_becomes_h3(self):
        notes = "# My Heading\nSome text."
        result = _adjust_notes_headings(notes, 2)
        self.assertIn("### My Heading", result)
        self.assertNotRegex(result, r"(?m)^# My Heading")

    def test_min_level_not_one(self):
        # Min is ##, so ## → ### and ### → ####
        notes = "## Section\n### Subsection"
        result = _adjust_notes_headings(notes, 2)
        self.assertIn("### Section", result)
        self.assertIn("#### Subsection", result)

    def test_mixed_levels_scaled_correctly(self):
        notes = "# Top\n## Second\n### Third"
        result = _adjust_notes_headings(notes, 2)
        self.assertIn("### Top", result)
        self.assertIn("#### Second", result)
        self.assertIn("##### Third", result)

    def test_heading_capped_at_h6(self):
        # item_heading_level=2 → target_base=3; min=1, so h5 → h5-1+3=h7 → capped h6
        notes = "# H1\n##### H5"
        result = _adjust_notes_headings(notes, 2)
        self.assertIn("### H1", result)
        self.assertIn("###### H5", result)

    def test_body_text_preserved(self):
        notes = "# Heading\n\nSome **bold** text.\n\n- item"
        result = _adjust_notes_headings(notes, 2)
        self.assertIn("Some **bold** text.", result)
        self.assertIn("- item", result)

    def test_hash_in_body_not_treated_as_heading(self):
        # Only lines where # is at the start are headings
        notes = "# Real heading\nThis has a # in the middle"
        result = _adjust_notes_headings(notes, 2)
        self.assertIn("### Real heading", result)
        self.assertIn("This has a # in the middle", result)


class ZipIndexHeadingAdjustmentTests(TestCase):
    """Integration: headings in item notes are re-levelled in the ZIP index."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "admin@example.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")
        self.fy = FinancialYear.objects.create(year=2024)

    def _get_index(self):
        url = reverse("admin:tracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        buf = io.BytesIO(response.content)
        with zipfile.ZipFile(buf) as zf:
            return zf.read("index.md").decode()

    def test_h1_in_notes_becomes_h3_in_index(self):
        Item.objects.create(
            year=self.fy,
            title="Coworking",
            notes="Intro text.\n\n# FY2024 (2 dates)\n\nSome detail.",
            order=1,
        )
        index = self._get_index()
        self.assertIn("### FY2024 (2 dates)", index)
        # The raw # heading must not appear at level 1 or 2
        self.assertNotIn("\n# FY2024", index)
        self.assertNotIn("\n## FY2024", index)

    def test_h2_min_in_notes_becomes_h3_in_index(self):
        Item.objects.create(
            year=self.fy,
            title="Section",
            notes="## Overview\n### Detail",
            order=1,
        )
        index = self._get_index()
        self.assertIn("### Overview", index)
        self.assertIn("#### Detail", index)


# ---------------------------------------------------------------------------
# archives.py unit tests
# ---------------------------------------------------------------------------

PDF_MAGIC = b"%PDF-1.4 fake pdf content"
FIDELITY_URL_PREFIX = (
    "https://netbenefitsww.fidelity.com"
    "/mybenefitsww/spshistoryservices/activities/record/c:"
)


def _make_fidelity_har(
    filename="statement-2023-07-20",
    pdf_bytes=None,
    method="GET",
    mime_type="application/json",
    body_encoding=None,
):
    """Build minimal Fidelity HAR bytes for testing."""
    if pdf_bytes is None:
        pdf_bytes = PDF_MAGIC
    file_content_b64 = base64.b64encode(pdf_bytes).decode()
    body_json = json.dumps({"fileContent": file_content_b64})
    if body_encoding == "base64":
        body_text = base64.b64encode(body_json.encode()).decode()
    else:
        body_text = body_json

    entry = {
        "request": {
            "method": method,
            "url": f"{FIDELITY_URL_PREFIX}{filename}",
        },
        "response": {
            "content": {
                "mimeType": mime_type,
                "text": body_text,
            },
        },
    }
    if body_encoding:
        entry["response"]["content"]["encoding"] = body_encoding

    har = {"log": {"entries": [entry]}}
    return json.dumps(har).encode()


def _make_zip_archive(entries):
    """Build ZIP bytes from (name, bytes) pairs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


class ArchiveExtractorTests(TestCase):
    """Unit tests for the archives.extract_from_archive helper."""

    # ------------------------------------------------------------------
    # Fidelity HAR — successful extraction
    # ------------------------------------------------------------------

    def test_har_extracts_pdf(self):
        har_bytes = _make_fidelity_har("statement-2023-07-20", PDF_MAGIC)
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(len(results), 1)
        name, data = results[0]
        self.assertEqual(name, "statement-2023-07-20.pdf")
        self.assertEqual(data, PDF_MAGIC)

    def test_har_base64_encoded_body(self):
        """HAR body itself Base64-encoded (body_encoding=base64)."""
        har_bytes = _make_fidelity_har(
            "statement-2023-07-20", PDF_MAGIC, body_encoding="base64"
        )
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], PDF_MAGIC)

    def test_har_filename_derived_from_url(self):
        har_bytes = _make_fidelity_har("mystatement", PDF_MAGIC)
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(results[0][0], "mystatement.pdf")

    def test_har_no_matching_entries(self):
        """A HAR with no Fidelity entries returns an empty list."""
        har = {"log": {"entries": []}}
        results = extract_from_archive(json.dumps(har).encode(), "empty.har")
        self.assertEqual(results, [])

    def test_har_skips_non_get_requests(self):
        har_bytes = _make_fidelity_har("statement-2023-07-20", PDF_MAGIC, method="POST")
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(results, [])

    def test_har_skips_wrong_mime_type(self):
        har_bytes = _make_fidelity_har(
            "statement-2023-07-20", PDF_MAGIC, mime_type="text/html"
        )
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(results, [])

    def test_har_skips_non_pdf_content(self):
        har_bytes = _make_fidelity_har("statement-2023-07-20", b"not a pdf")
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(results, [])

    def test_har_path_traversal_sanitized(self):
        """A URL suffix with path separators must not escape the output path."""
        har_bytes = _make_fidelity_har("../../../evil", PDF_MAGIC)
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(len(results), 1)
        name, _ = results[0]
        # basename only — no directory components
        self.assertEqual(name, "evil.pdf")
        self.assertNotIn("..", name)
        self.assertNotIn("/", name)

    def test_zip_extracts_pdf_names_and_normalises_uppercase_extension(self):
        zip_bytes = _make_zip_archive(
            [
                ("folder/statement.PDF", PDF_MAGIC),
                ("nested/sub/report.pdf", PDF_MAGIC),
                ("ignored.txt", b"text"),
            ]
        )
        results = extract_from_archive(zip_bytes, "export.zip")
        self.assertEqual(
            [name for name, _ in results],
            ["statement.pdf", "report.pdf"],
        )

    def test_zip_reports_non_pdf_entries_as_skipped(self):
        zip_bytes = _make_zip_archive(
            [
                ("folder/statement.pdf", PDF_MAGIC),
                ("ignored.txt", b"text"),
            ]
        )
        _, skipped = extract_from_archive_with_skips(zip_bytes, "export.zip")
        self.assertEqual(skipped, ["ignored.txt"])

    def test_invalid_json_raises_unsupported(self):
        with self.assertRaises(UnsupportedArchiveError):
            extract_from_archive(b"not json at all", "export.har")

    # ------------------------------------------------------------------
    # Unsupported format
    # ------------------------------------------------------------------

    def test_unknown_extension_raises(self):
        with self.assertRaises(UnsupportedArchiveError):
            extract_from_archive(b"anything", "archive.7z")

    def test_no_extension_raises(self):
        with self.assertRaises(UnsupportedArchiveError):
            extract_from_archive(b"anything", "noextension")


# ---------------------------------------------------------------------------
# Import archive admin view tests
# ---------------------------------------------------------------------------


class ImportArchiveViewTests(TestCase):
    """Tests for the Item admin 'Import Archive' view."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Test Item", order=1)

    def _import_url(self):
        return reverse("admin:tracker_item_import_archive", args=[self.item.pk])

    # ------------------------------------------------------------------
    # GET — show upload form
    # ------------------------------------------------------------------

    def test_get_shows_form(self):
        response = self.client.get(self._import_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import Archive")
        self.assertContains(response, "Test Item")

    def test_change_form_has_import_archive_link(self):
        url = reverse("admin:tracker_item_change", args=[self.item.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import Archive")
        self.assertContains(response, self._import_url())

    # ------------------------------------------------------------------
    # POST — valid HAR → attachments created
    # ------------------------------------------------------------------

    def test_post_valid_har_creates_attachments(self):
        har_bytes = _make_fidelity_har("statement-2023-07-20", PDF_MAGIC)
        upload = io.BytesIO(har_bytes)
        upload.name = "export.har"
        before = Attachment.objects.filter(item=self.item).count()
        response = self.client.post(
            self._import_url(),
            {"archive": upload},
        )
        self.assertRedirects(
            response,
            reverse("admin:tracker_item_change", args=[self.item.pk]),
        )
        self.assertEqual(Attachment.objects.filter(item=self.item).count(), before + 1)

    def test_post_valid_har_date_extracted(self):
        """Date is auto-extracted from the derived filename."""
        har_bytes = _make_fidelity_har("statement-2023-07-20", PDF_MAGIC)
        upload = io.BytesIO(har_bytes)
        upload.name = "export.har"
        self.client.post(self._import_url(), {"archive": upload})
        att = Attachment.objects.filter(item=self.item).latest("pk")
        self.assertEqual(att.date, datetime.date(2023, 7, 20))

    def test_post_har_with_no_entries_shows_warning(self):
        har = {"log": {"entries": []}}
        upload = io.BytesIO(json.dumps(har).encode())
        upload.name = "empty.har"
        response = self.client.post(
            self._import_url(),
            {"archive": upload},
            follow=True,
        )
        self.assertContains(response, "No attachments could be extracted")

    def test_post_unsupported_format_shows_error(self):
        upload = io.BytesIO(b"anything")
        upload.name = "archive.7z"
        response = self.client.post(
            self._import_url(),
            {"archive": upload},
            follow=True,
        )
        self.assertContains(response, "Unrecognised archive format")

    def test_post_zip_skips_non_pdf_files_and_shows_warning(self):
        zip_bytes = _make_zip_archive(
            [
                ("folder/statement.PDF", PDF_MAGIC),
                ("nested/report.pdf", PDF_MAGIC),
                ("ignored.txt", b"text"),
            ]
        )
        upload = io.BytesIO(zip_bytes)
        upload.name = "archive.zip"
        response = self.client.post(
            self._import_url(),
            {"archive": upload},
            follow=True,
        )
        self.assertContains(response, "Imported 2 attachment(s)")
        self.assertContains(response, "Skipped 1 file(s): ignored.txt")
        self.assertTrue(
            Attachment.objects.filter(
                item=self.item, file__endswith="/statement.pdf"
            ).exists()
        )
        self.assertTrue(
            Attachment.objects.filter(
                item=self.item, file__endswith="/report.pdf"
            ).exists()
        )

    def test_post_zip_skips_existing_filename_any_item_and_warns(self):
        other_item = Item.objects.create(year=self.fy, title="Other Item", order=2)
        existing = Attachment(item=other_item)
        existing.file.save("existing.pdf", ContentFile(PDF_MAGIC), save=False)
        existing.save()

        zip_bytes = _make_zip_archive(
            [
                ("existing.pdf", PDF_MAGIC),
                ("new.pdf", PDF_MAGIC),
            ]
        )
        upload = io.BytesIO(zip_bytes)
        upload.name = "archive.zip"
        response = self.client.post(
            self._import_url(),
            {"archive": upload},
            follow=True,
        )
        self.assertContains(response, "Imported 1 attachment(s)")
        self.assertContains(response, "Skipped 1 file(s): existing.pdf")
        self.assertTrue(
            Attachment.objects.filter(
                item=self.item, file__endswith="/new.pdf"
            ).exists()
        )
        self.assertFalse(
            Attachment.objects.filter(
                item=self.item, file__endswith="/existing.pdf"
            ).exists()
        )

    def test_post_missing_file_shows_form_error(self):
        """Posting without a file should re-render the form with an error."""
        response = self.client.post(self._import_url(), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")

    # ------------------------------------------------------------------
    # Permission checks
    # ------------------------------------------------------------------

    def _limited_client(self, username, *permission_codenames):
        """Return a Client logged in as a staff user with only the given permissions."""
        from django.contrib.auth.models import Permission

        user = User.objects.create_user(username, f"{username}@b.com", "pass")
        user.is_staff = True
        user.save()
        for codename in permission_codenames:
            perm = Permission.objects.get(codename=codename)
            user.user_permissions.add(perm)
        client = Client()
        client.login(username=username, password="pass")
        return client

    def test_get_requires_item_change_permission(self):
        """Staff user without change_item permission should get 403."""
        client = self._limited_client("limited1", "add_attachment")
        response = client.get(self._import_url())
        self.assertEqual(response.status_code, 403)

    def test_get_requires_attachment_add_permission(self):
        """Staff user without add_attachment permission should get 403."""
        client = self._limited_client("limited2", "change_item")
        response = client.get(self._import_url())
        self.assertEqual(response.status_code, 403)

    def test_get_allowed_with_both_permissions(self):
        """Staff user with both change_item and add_attachment can access view."""
        client = self._limited_client("limited3", "change_item", "add_attachment")
        response = client.get(self._import_url())
        self.assertEqual(response.status_code, 200)
