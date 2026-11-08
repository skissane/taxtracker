import datetime
import io
import json
import zipfile
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core.files.base import ContentFile
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from lifetracker.taxtracker.admin import _attachment_date_warning
from lifetracker.taxtracker.models import Attachment, FinancialYear, Item, ReceivedDocument
from tests.taxtracker.test_archives import PDF_MAGIC, _make_fidelity_har, _make_zip_archive


class CopyItemsTests(TestCase):
    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)
        self.root = Item.objects.create(
            year=self.fy,
            title="Root",
            order=1,
            notes="some note",
            status=Item.STATUS_DONE,
        )
        self.child = Item.objects.create(year=self.fy, parent=self.root, title="Child", order=1)

    def _create_superuser_client(self) -> Client:
        User.objects.create_superuser("admin", "admin@example.com", "password")
        client = Client()
        client.login(username="admin", password="password")
        return client

    def _create_cycle(self) -> tuple[Item, Item]:
        first = Item.objects.create(year=self.fy, title="Cycle A", order=9)
        second = Item.objects.create(year=self.fy, parent=first, title="Cycle B", order=1)
        first.parent = second
        first.save(update_fields=["parent"])
        return first, second

    def test_copy_to_new_year_creates_items(self) -> None:
        client = self._create_superuser_client()
        url = reverse("admin:taxtracker_financialyear_copy_to_new_year", args=[self.fy.pk])
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

    def test_copy_existing_year_shows_error(self) -> None:
        FinancialYear.objects.create(year=2025)
        client = self._create_superuser_client()
        url = reverse("admin:taxtracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        response = client.post(url)
        # Should redirect back with error message
        self.assertEqual(response.status_code, 302)
        self.assertFalse(FinancialYear.objects.filter(year=2026).exists())

    def test_copy_to_new_year_race_shows_existing_year_error(self) -> None:
        # Another request creates FY2025 after this one's exists() check, so
        # the create hits the unique constraint (issue #156).
        FinancialYear.objects.create(year=2025)
        client = self._create_superuser_client()
        url = reverse("admin:taxtracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        with mock.patch.object(FinancialYear.objects, "filter") as mock_filter:
            mock_filter.return_value.exists.return_value = False
            response = client.post(url)
        self.assertRedirects(
            response,
            reverse("admin:taxtracker_financialyear_changelist"),
            fetch_redirect_response=False,
        )
        self.assertContains(client.get(response["Location"]), "FY2025 already exists.")
        new_fy = FinancialYear.objects.get(year=2025)
        self.assertFalse(new_fy.items.exists())

    def test_copy_to_new_year_warns_about_items_unreachable_from_root(self) -> None:
        cycle_a, cycle_b = self._create_cycle()
        client = self._create_superuser_client()
        url = reverse("admin:taxtracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        response = client.get(url)
        self.assertContains(response, "not reachable from a root item")
        self.assertContains(response, cycle_a.title)
        self.assertContains(response, cycle_b.title)

    def test_copy_to_new_year_skips_unreachable_items_with_warning(self) -> None:
        self._create_cycle()
        client = self._create_superuser_client()
        url = reverse("admin:taxtracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        response = client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "not copied because they are not reachable from a root item",
        )
        new_fy = FinancialYear.objects.get(year=2025)
        self.assertEqual(
            list(new_fy.items.order_by("order", "title").values_list("title", flat=True)),
            ["Child", "Root"],
        )


class ReceivedDocumentAdminViewTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client = Client()
        self.client.login(username="admin", password="password")
        self.fy = FinancialYear.objects.create(year=2024)
        self.doc = ReceivedDocument.objects.create(year=self.fy, title="NOA")

    def _add_url(self) -> str:
        return reverse("admin:taxtracker_receiveddocument_add")

    def _change_url(self, doc: ReceivedDocument) -> str:
        return reverse("admin:taxtracker_receiveddocument_change", args=[doc.pk])

    def _base_post_data(self, **overrides: object) -> dict[str, object]:
        data: dict[str, object] = {
            "year": self.fy.pk,
            "title": "NOA",
            "date": "",
            "notes": "",
            "attachments-TOTAL_FORMS": "0",
            "attachments-INITIAL_FORMS": "0",
            "attachments-MIN_NUM_FORMS": "0",
            "attachments-MAX_NUM_FORMS": "1000",
        }
        data.update(overrides)
        return data

    def test_changelist(self) -> None:
        url = reverse("admin:taxtracker_receiveddocument_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "NOA")

    def test_add_view_get(self) -> None:
        response = self.client.get(self._add_url())
        self.assertEqual(response.status_code, 200)

    def test_add_creates_received_document(self) -> None:
        response = self.client.post(self._add_url(), self._base_post_data(title="New Doc"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ReceivedDocument.objects.filter(title="New Doc").exists())

    def test_changelist_shows_year_link(self) -> None:
        url = reverse("admin:taxtracker_receiveddocument_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        year_url = reverse("admin:taxtracker_financialyear_change", args=[self.fy.pk])
        self.assertContains(response, year_url)

    def test_inline_attachment_upload_sets_received_document(self) -> None:
        upload = io.BytesIO(b"%PDF-1.4 test")
        upload.name = "noa.pdf"
        data = self._base_post_data(
            **{
                "attachments-TOTAL_FORMS": "1",
                "attachments-0-id": "",
                "attachments-0-title": "",
                "attachments-0-date": "",
                "attachments-0-notes": "",
                "attachments-0-file": upload,
            }
        )
        response = self.client.post(self._change_url(self.doc), data)
        self.assertEqual(response.status_code, 302)
        att = Attachment.objects.get(received_document=self.doc)
        self.assertIsNone(att.item_id)
        self.assertEqual(att.received_document_id, self.doc.pk)

    def test_change_view_shows_edit_link_for_existing_attachment(self) -> None:
        att = Attachment.objects.create(
            received_document=self.doc,
            file=ContentFile(b"data", name="noa.pdf"),
        )
        response = self.client.get(self._change_url(self.doc))
        self.assertEqual(response.status_code, 200)
        attachment_url = reverse("admin:taxtracker_attachment_change", args=[att.pk])
        self.assertContains(response, attachment_url)


class AdminLoginMixin(SimpleTestCase):
    """Logs a superuser in, with one financial year and one item, for the admin view tests."""

    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client = Client()
        self.client.login(username="admin", password="password")
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Test Item", order=1)


class AdminViewTests(AdminLoginMixin, TestCase):
    def _limited_client(self, username: str, *permission_codenames: str) -> Client:
        user = User.objects.create_user(username, f"{username}@example.com", "password")
        user.is_staff = True
        user.save()
        for codename in permission_codenames:
            user.user_permissions.add(Permission.objects.get(codename=codename))
        client = Client()
        client.force_login(user)
        return client

    def test_changelist(self) -> None:
        url = reverse("admin:taxtracker_financialyear_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_summary_view(self) -> None:
        url = reverse("admin:taxtracker_financialyear_summary", args=[self.fy.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FY2024")
        self.assertContains(response, "Test Item")

    def test_summary_view_lists_items_unreachable_from_root(self) -> None:
        first = Item.objects.create(year=self.fy, title="Cycle A", order=9)
        second = Item.objects.create(year=self.fy, parent=first, title="Cycle B", order=1)
        first.parent = second
        first.save(update_fields=["parent"])

        url = reverse("admin:taxtracker_financialyear_summary", args=[self.fy.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not reachable from a root item")
        self.assertContains(response, first.title)
        self.assertContains(response, second.title)

    def test_summary_view_requires_financialyear_view_permission(self) -> None:
        client = self._limited_client("limited-summary")
        url = reverse("admin:taxtracker_financialyear_summary", args=[self.fy.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_download_zip_view(self) -> None:
        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        buf = io.BytesIO(response.getvalue())
        with zipfile.ZipFile(buf) as zf:
            self.assertIn("index.md", zf.namelist())
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="FY2024_attachments.zip"',
        )

    def test_download_zip_view_requires_financialyear_view_permission(self) -> None:
        client = self._limited_client("limited-zip")
        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_copy_to_new_year_requires_financialyear_add_permission(self) -> None:
        client = self._limited_client("limited-copy-add", "view_financialyear")
        url = reverse("admin:taxtracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_copy_to_new_year_requires_financialyear_view_permission(self) -> None:
        client = self._limited_client("limited-copy-view", "add_financialyear")
        url = reverse("admin:taxtracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_download_zip_view_requires_item_and_attachment_view_permission(self) -> None:
        """A ZIP holds items' notes and attachments' files: FY view isn't enough."""
        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        for username, codenames in [
            ("zip-fy-only", ["view_financialyear"]),
            ("zip-no-attachment", ["view_financialyear", "view_item"]),
            ("zip-no-item", ["view_financialyear", "view_attachment"]),
        ]:
            with self.subTest(codenames=codenames):
                client = self._limited_client(username, *codenames)
                self.assertEqual(client.get(url).status_code, 403)

    def test_download_zip_view_allowed_with_view_permissions(self) -> None:
        client = self._limited_client("zip-all", "view_financialyear", "view_item", "view_attachment")
        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.getvalue())) as zf:
            self.assertIn("index.md", zf.namelist())

    def test_download_multi_zip_requires_attachment_view_permission(self) -> None:
        client = self._limited_client("multi-no-attachment", "view_financialyear", "view_item")
        url = reverse("admin:taxtracker_financialyear_download_multi_zip")
        self.assertEqual(client.get(url).status_code, 403)
        response = client.post(url, {"fy_ids": [self.fy.pk], "action": "view_index"})
        self.assertEqual(response.status_code, 403)

    def test_copy_to_new_year_requires_item_add_permission(self) -> None:
        client = self._limited_client("copy-no-item", "view_financialyear", "add_financialyear")
        url = reverse("admin:taxtracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        self.assertEqual(client.post(url).status_code, 403)
        self.assertFalse(FinancialYear.objects.filter(year=2025).exists())

    def test_copy_to_new_year_allowed_with_add_permissions(self) -> None:
        client = self._limited_client("copy-all", "view_financialyear", "add_financialyear", "add_item")
        url = reverse("admin:taxtracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        response = client.post(url)
        self.assertEqual(response.status_code, 302)
        new_fy = FinancialYear.objects.get(year=2025)
        self.assertEqual(list(new_fy.items.values_list("title", flat=True)), ["Test Item"])

    def test_download_multi_zip_get(self) -> None:
        """GET renders the multi-year ZIP selection form."""
        url = reverse("admin:taxtracker_financialyear_download_multi_zip")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FY2024")

    def test_download_multi_zip_get_default_selection_uses_status(self) -> None:
        submitted = FinancialYear.objects.create(
            year=2025,
            status=FinancialYear.STATUS_SUBMITTED,
        )
        more_info = FinancialYear.objects.create(
            year=2026,
            status=FinancialYear.STATUS_MORE_INFO_REQUESTED,
        )
        finalised = FinancialYear.objects.create(
            year=2027,
            status=FinancialYear.STATUS_FINALISED,
        )

        url = reverse("admin:taxtracker_financialyear_download_multi_zip")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        self.assertIn("Pending submission to tax agent", content)
        self.assertIn("Submitted to tax agent", content)
        self.assertIn("Tax agent requests more info", content)
        self.assertIn("Finalised", content)
        self.assertRegex(content, rf'<input[^>]*value="{self.fy.pk}"[^>]*checked')
        self.assertNotRegex(
            content,
            rf'<input[^>]*value="{submitted.pk}"[^>]*checked',
        )
        self.assertRegex(content, rf'<input[^>]*value="{more_info.pk}"[^>]*checked')
        self.assertNotRegex(
            content,
            rf'<input[^>]*value="{finalised.pk}"[^>]*checked',
        )

    def test_download_multi_zip_filename(self) -> None:
        fy2026 = FinancialYear.objects.create(year=2026)
        url = reverse("admin:taxtracker_financialyear_download_multi_zip")
        for fy_ids, expected in [
            ([self.fy.pk], "FY2024_attachments.zip"),
            ([fy2026.pk, self.fy.pk], "FY2024-FY2026_multi_attachments.zip"),
        ]:
            with self.subTest(expected=expected):
                response = self.client.post(url, {"fy_ids": fy_ids, "action": "zip"})

                self.assertIn(f'filename="{expected}"', response["Content-Disposition"])

    def test_download_multi_zip_post_returns_zip(self) -> None:
        """POST with action=zip returns a ZIP with a per-year top-level prefix."""
        fy2025 = FinancialYear.objects.create(year=2025)
        item2025 = Item.objects.create(year=fy2025, title="Item 2025", order=1)
        Attachment.objects.create(
            item=item2025,
            title="doc2025.pdf",
            file=ContentFile(b"data", name="doc2025.pdf"),
        )
        url = reverse("admin:taxtracker_financialyear_download_multi_zip")
        response = self.client.post(
            url,
            {"fy_ids": [self.fy.pk, fy2025.pk], "action": "zip"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        buf = io.BytesIO(response.getvalue())
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

    def test_download_multi_zip_post_view_index(self) -> None:
        """POST with action=view_index returns markdown containing FY headers."""
        fy2025 = FinancialYear.objects.create(year=2025)
        url = reverse("admin:taxtracker_financialyear_download_multi_zip")
        response = self.client.post(
            url,
            {"fy_ids": [self.fy.pk, fy2025.pk], "action": "view_index"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/markdown", response["Content-Type"])
        content = response.content.decode()
        self.assertIn("FY2024", content)
        self.assertIn("FY2025", content)

    def test_download_multi_zip_post_invalid_fy_ids_shows_error(self) -> None:
        url = reverse("admin:taxtracker_financialyear_download_multi_zip")
        response = self.client.post(url, {"fy_ids": ["abc", "99999999999999999999"], "action": "zip"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please select at least one financial year.")

    def test_financialyear_change_view_non_numeric_id_uses_admin_redirect(self) -> None:
        response = self.client.get("/admin/taxtracker/financialyear/abc/change/")
        self.assertRedirects(response, reverse("admin:index"))

    def test_change_views_accept_ids_the_admin_normalises(self) -> None:
        """The admin's lookup accepts "+1"; the change forms' links must still reverse."""
        for url, link in [
            (
                f"/admin/taxtracker/financialyear/+{self.fy.pk}/change/",
                reverse("admin:taxtracker_financialyear_summary", args=[self.fy.pk]),
            ),
            (
                f"/admin/taxtracker/item/+{self.item.pk}/change/",
                reverse("admin:taxtracker_item_import_archive", args=[self.item.pk]),
            ),
        ]:
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), link)

    def test_financialyear_change_view_links_summary_and_adjacent_years(self) -> None:
        prev_fy = FinancialYear.objects.create(year=2023)
        next_fy = FinancialYear.objects.create(year=2025)
        response = self.client.get(reverse("admin:taxtracker_financialyear_change", args=[self.fy.pk]))
        self.assertContains(response, reverse("admin:taxtracker_financialyear_summary", args=[self.fy.pk]))
        self.assertContains(response, reverse("admin:taxtracker_financialyear_change", args=[prev_fy.pk]))
        self.assertContains(response, reverse("admin:taxtracker_financialyear_change", args=[next_fy.pk]))

    def test_item_changelist_shows_year_link(self) -> None:
        url = reverse("admin:taxtracker_item_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("admin:taxtracker_financialyear_change", args=[self.fy.pk]),
        )

    def test_copy_to_new_year_get(self) -> None:
        url = reverse("admin:taxtracker_financialyear_copy_to_new_year", args=[self.fy.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FY2025")

    def test_reassign_attachments_view_lists_movable_and_unmovable(self) -> None:
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

        url = reverse("admin:taxtracker_item_reassign_attachments", args=[self.item.pk])
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

    def test_item_change_view_links_to_equivalent_prev_and_next_items(self) -> None:
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

        url = reverse("admin:taxtracker_item_change", args=[current_item.pk])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("admin:taxtracker_item_change", args=[prev_item.pk]),
        )
        self.assertContains(response, "← Previous Year (FY2023)")
        self.assertContains(
            response,
            reverse("admin:taxtracker_item_change", args=[next_item.pk]),
        )
        self.assertContains(response, "Next Year (FY2025) →")

    def test_item_change_view_missing_object_uses_admin_redirect(self) -> None:
        url = reverse("admin:taxtracker_item_change", args=[999999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_reassign_attachments_view_ignores_invalid_ids(self) -> None:
        fy2025 = FinancialYear.objects.create(year=2025)
        target = Item.objects.create(year=fy2025, title=self.item.title, order=self.item.order)
        attachment = Attachment.objects.create(
            item=self.item,
            title="to-move.pdf",
            date=datetime.date(2024, 8, 2),
            file=ContentFile(b"to-move", name="to-move.pdf"),
        )

        url = reverse("admin:taxtracker_item_reassign_attachments", args=[self.item.pk])
        # "²" is a digit to str.isdigit() but not to int().
        response = self.client.post(url, data={"move_attachment_ids": ["abc", "²", "-1", str(attachment.pk)]})

        self.assertEqual(response.status_code, 200)
        attachment.refresh_from_db()
        self.assertEqual(attachment.item_id, target.pk)

    def test_reassign_attachments_view_moves_selected(self) -> None:
        fy2025 = FinancialYear.objects.create(year=2025)
        fy2023 = FinancialYear.objects.create(year=2023)
        target_2025 = Item.objects.create(year=fy2025, title=self.item.title, order=self.item.order)
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

        url = reverse("admin:taxtracker_item_reassign_attachments", args=[self.item.pk])
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

    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "admin@example.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")
        self.fy = FinancialYear.objects.create(year=2024)
        self.parent_item = Item.objects.create(year=self.fy, title="Parent", order=1)

    def _item_change_url(self, item: Item) -> str:
        return reverse("admin:taxtracker_item_change", args=[item.pk])

    def test_inline_child_inherits_year_on_save(self) -> None:
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

    def test_year_field_hidden_for_child_item(self) -> None:
        """Year field should not appear in the change form for child items."""
        child = Item.objects.create(year=self.fy, parent=self.parent_item, title="Child", order=1)
        url = self._item_change_url(child)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # The year select widget should NOT be rendered for child items.
        self.assertNotContains(response, 'id="id_year"')

    def test_year_field_present_for_root_item(self) -> None:
        """Year field must appear in the change form for root items."""
        url = self._item_change_url(self.parent_item)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="id_year"')

    def test_cascade_year_on_root_item_save(self) -> None:
        """Changing a root item's year must cascade to all descendants."""
        fy2025 = FinancialYear.objects.create(year=2025)
        child = Item.objects.create(year=self.fy, parent=self.parent_item, title="Child", order=1)
        grandchild = Item.objects.create(year=self.fy, parent=child, title="Grandchild", order=1)

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


class AttachmentDateWarningHelperTests(TestCase):
    """Tests for _attachment_date_warning covering both Item and
    ReceivedDocument parents."""

    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Income", order=1)
        self.doc = ReceivedDocument.objects.create(year=self.fy, title="NOA")

    def test_item_date_in_range_no_warning(self) -> None:
        att = Attachment.objects.create(
            item=self.item,
            date=datetime.date(2024, 1, 1),
            file=ContentFile(b"data", name="in_range.pdf"),
        )
        self.assertEqual(_attachment_date_warning(att), "")

    def test_item_date_out_of_range_warns(self) -> None:
        att = Attachment.objects.create(
            item=self.item,
            date=datetime.date(2020, 1, 1),
            file=ContentFile(b"data", name="out_of_range.pdf"),
        )
        self.assertIn("outside the financial year", _attachment_date_warning(att))

    def test_received_document_date_in_range_no_warning(self) -> None:
        att = Attachment.objects.create(
            received_document=self.doc,
            date=datetime.date(2024, 1, 1),
            file=ContentFile(b"data", name="in_range.pdf"),
        )
        self.assertEqual(_attachment_date_warning(att), "")

    def test_received_document_date_out_of_range_warns(self) -> None:
        att = Attachment.objects.create(
            received_document=self.doc,
            date=datetime.date(2020, 1, 1),
            file=ContentFile(b"data", name="out_of_range.pdf"),
        )
        self.assertIn("outside the financial year", _attachment_date_warning(att))

    def test_neither_parent_returns_empty(self) -> None:
        """Not reachable for a saved row (the CheckConstraint forbids it),
        but the helper is written defensively -- exercise it directly."""
        att = Attachment(date=datetime.date(2024, 1, 1))
        att.pk = 12345
        self.assertEqual(_attachment_date_warning(att), "")


# ---------------------------------------------------------------------------
# Import archive admin view tests
# ---------------------------------------------------------------------------


class ImportArchiveViewTests(TestCase):
    """Tests for the Item admin 'Import Archive' view."""

    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Test Item", order=1)

    def _import_url(self) -> str:
        return reverse("admin:taxtracker_item_import_archive", args=[self.item.pk])

    # ------------------------------------------------------------------
    # GET — show upload form
    # ------------------------------------------------------------------

    def test_get_shows_form(self) -> None:
        response = self.client.get(self._import_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import Archive")
        self.assertContains(response, "Test Item")

    def test_change_form_has_import_archive_link(self) -> None:
        url = reverse("admin:taxtracker_item_change", args=[self.item.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import Archive")
        self.assertContains(response, self._import_url())

    # ------------------------------------------------------------------
    # POST — valid HAR → attachments created
    # ------------------------------------------------------------------

    def test_post_valid_har_creates_attachments(self) -> None:
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
            reverse("admin:taxtracker_item_change", args=[self.item.pk]),
        )
        self.assertEqual(Attachment.objects.filter(item=self.item).count(), before + 1)

    def test_post_valid_har_date_extracted(self) -> None:
        """Date is auto-extracted from the derived filename."""
        har_bytes = _make_fidelity_har("statement-2023-07-20", PDF_MAGIC)
        upload = io.BytesIO(har_bytes)
        upload.name = "export.har"
        self.client.post(self._import_url(), {"archive": upload})
        att = Attachment.objects.filter(item=self.item).latest("pk")
        self.assertEqual(att.date, datetime.date(2023, 7, 20))

    def test_post_har_with_no_entries_shows_warning(self) -> None:
        har: dict[str, object] = {"log": {"entries": []}}
        upload = io.BytesIO(json.dumps(har).encode())
        upload.name = "empty.har"
        response = self.client.post(
            self._import_url(),
            {"archive": upload},
            follow=True,
        )
        self.assertContains(response, "No attachments could be extracted")

    def test_post_unsupported_format_shows_error(self) -> None:
        upload = io.BytesIO(b"anything")
        upload.name = "archive.7z"
        response = self.client.post(
            self._import_url(),
            {"archive": upload},
            follow=True,
        )
        self.assertContains(response, "Unrecognised archive format")

    def test_post_zip_skips_non_pdf_files_and_shows_warning(self) -> None:
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
        self.assertContains(response, "Skipped 1 non-PDF, too large or unreadable file(s): ignored.txt")
        self.assertTrue(Attachment.objects.filter(item=self.item, file__endswith="/statement.pdf").exists())
        self.assertTrue(Attachment.objects.filter(item=self.item, file__endswith="/report.pdf").exists())

    def test_post_zip_allows_existing_filename_on_other_item(self) -> None:
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
        self.assertContains(response, "Imported 2 attachment(s)")
        self.assertTrue(Attachment.objects.filter(item=self.item, file__endswith="/new.pdf").exists())
        self.assertTrue(Attachment.objects.filter(item=self.item, file__endswith="/existing.pdf").exists())

    def test_post_zip_skips_existing_filename_on_same_item(self) -> None:
        existing = Attachment(item=self.item)
        existing.file.save("existing.pdf", ContentFile(PDF_MAGIC), save=False)
        existing.save()

        zip_bytes = _make_zip_archive([("existing.pdf", PDF_MAGIC)])
        upload = io.BytesIO(zip_bytes)
        upload.name = "archive.zip"
        response = self.client.post(
            self._import_url(),
            {"archive": upload},
            follow=True,
        )
        self.assertContains(response, "Skipped 1 file(s) already attached to this item: existing.pdf")

    def test_post_missing_file_shows_form_error(self) -> None:
        """Posting without a file should re-render the form with an error."""
        response = self.client.post(self._import_url(), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")

    # ------------------------------------------------------------------
    # Permission checks
    # ------------------------------------------------------------------

    def _limited_client(self, username: str, *permission_codenames: str) -> Client:
        """Return a Client logged in as a staff user with only the given permissions."""
        user = User.objects.create_user(username, f"{username}@b.com", "pass")
        user.is_staff = True
        user.save()
        for codename in permission_codenames:
            perm = Permission.objects.get(codename=codename)
            user.user_permissions.add(perm)
        client = Client()
        client.login(username=username, password="pass")
        return client

    def test_get_requires_item_change_permission(self) -> None:
        """Staff user without change_item permission should get 403."""
        client = self._limited_client("limited1", "add_attachment")
        response = client.get(self._import_url())
        self.assertEqual(response.status_code, 403)

    def test_get_requires_attachment_add_permission(self) -> None:
        """Staff user without add_attachment permission should get 403."""
        client = self._limited_client("limited2", "change_item")
        response = client.get(self._import_url())
        self.assertEqual(response.status_code, 403)

    def test_get_allowed_with_both_permissions(self) -> None:
        """Staff user with both change_item and add_attachment can access view."""
        client = self._limited_client("limited3", "change_item", "add_attachment")
        response = client.get(self._import_url())
        self.assertEqual(response.status_code, 200)


class ReceivedDocumentImportArchiveViewTests(TestCase):
    """Tests for the ReceivedDocument admin 'Import Archive' view."""

    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")
        self.fy2024 = FinancialYear.objects.create(year=2024)

    def _import_url(self) -> str:
        return reverse("admin:taxtracker_receiveddocument_import_archive")

    def _changelist_url(self) -> str:
        return reverse("admin:taxtracker_receiveddocument_changelist")

    # ------------------------------------------------------------------
    # GET — show upload form
    # ------------------------------------------------------------------

    def test_get_shows_form(self) -> None:
        response = self.client.get(self._import_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import Archive")

    def test_changelist_has_import_archive_link(self) -> None:
        response = self.client.get(self._changelist_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import Archive")
        self.assertContains(response, self._import_url())

    # ------------------------------------------------------------------
    # POST — valid ZIP → received documents created
    # ------------------------------------------------------------------

    def test_post_zip_creates_received_documents_per_year(self) -> None:
        fy2025 = FinancialYear.objects.create(year=2025)
        zip_bytes = _make_zip_archive(
            [
                ("2024-07-20-notice.pdf", PDF_MAGIC),
                ("2025_assessment.PDF", PDF_MAGIC),
            ]
        )
        upload = io.BytesIO(zip_bytes)
        upload.name = "archive.zip"
        response = self.client.post(self._import_url(), {"archive": upload}, follow=True)
        self.assertContains(response, "Imported 2 received document(s)")

        doc_2024 = ReceivedDocument.objects.get(title="2024-07-20-notice.pdf")
        self.assertEqual(doc_2024.year, self.fy2024)
        self.assertEqual(doc_2024.date, datetime.date(2024, 7, 20))
        self.assertTrue(
            Attachment.objects.filter(received_document=doc_2024, file__endswith="/2024-07-20-notice.pdf").exists()
        )

        doc_2025 = ReceivedDocument.objects.get(title="2025_assessment.pdf")
        self.assertEqual(doc_2025.year, fy2025)

    def test_post_zip_skips_non_pdf_files(self) -> None:
        zip_bytes = _make_zip_archive(
            [
                ("2024-notice.pdf", PDF_MAGIC),
                ("ignored.txt", b"text"),
            ]
        )
        upload = io.BytesIO(zip_bytes)
        upload.name = "archive.zip"
        response = self.client.post(self._import_url(), {"archive": upload}, follow=True)
        self.assertContains(response, "Imported 1 received document(s)")
        self.assertContains(response, "Skipped 1 non-PDF, too large or unreadable file(s): ignored.txt")

    def test_post_zip_skips_pdf_without_year_prefix(self) -> None:
        zip_bytes = _make_zip_archive(
            [
                ("2024-notice.pdf", PDF_MAGIC),
                ("notes.pdf", PDF_MAGIC),
                ("12345.pdf", PDF_MAGIC),
            ]
        )
        upload = io.BytesIO(zip_bytes)
        upload.name = "archive.zip"
        response = self.client.post(self._import_url(), {"archive": upload}, follow=True)
        self.assertContains(response, "Imported 1 received document(s)")
        self.assertContains(
            response,
            "Skipped 2 file(s) without a leading 4-digit year: notes.pdf, 12345.pdf",
        )

    def test_post_zip_skips_pdf_with_missing_financial_year(self) -> None:
        zip_bytes = _make_zip_archive([("2019-letter.pdf", PDF_MAGIC)])
        upload = io.BytesIO(zip_bytes)
        upload.name = "archive.zip"
        response = self.client.post(self._import_url(), {"archive": upload}, follow=True)
        self.assertContains(
            response,
            "Skipped 1 file(s) with no matching Financial Year record: 2019-letter.pdf (FY2019)",
        )
        self.assertFalse(FinancialYear.objects.filter(year=2019).exists())
        self.assertFalse(ReceivedDocument.objects.filter(title="2019-letter.pdf").exists())

    def test_post_zip_skips_existing_filename_and_warns(self) -> None:
        other_doc = ReceivedDocument.objects.create(year=self.fy2024, title="Existing")
        existing = Attachment(received_document=other_doc)
        existing.file.save("2024-existing.pdf", ContentFile(PDF_MAGIC), save=False)
        existing.save()

        zip_bytes = _make_zip_archive(
            [
                ("2024-existing.pdf", PDF_MAGIC),
                ("2024-new.pdf", PDF_MAGIC),
            ]
        )
        upload = io.BytesIO(zip_bytes)
        upload.name = "archive.zip"
        response = self.client.post(self._import_url(), {"archive": upload}, follow=True)
        self.assertContains(response, "Imported 1 received document(s)")
        self.assertContains(
            response,
            "Skipped 1 file(s) already imported for that Financial Year: 2024-existing.pdf",
        )
        self.assertTrue(ReceivedDocument.objects.filter(title="2024-new.pdf").exists())

    def test_post_zip_allows_existing_filename_on_item(self) -> None:
        # The file's year prefix picks its FY, so only another received
        # document in that FY can hold the same name; an item's can't block it.
        item = Item.objects.create(year=self.fy2024, title="Item", order=1)
        existing = Attachment(item=item)
        existing.file.save("2024-existing.pdf", ContentFile(PDF_MAGIC), save=False)
        existing.save()

        zip_bytes = _make_zip_archive([("2024-existing.pdf", PDF_MAGIC)])
        upload = io.BytesIO(zip_bytes)
        upload.name = "archive.zip"
        response = self.client.post(self._import_url(), {"archive": upload}, follow=True)
        self.assertContains(response, "Imported 1 received document(s)")
        self.assertTrue(ReceivedDocument.objects.filter(title="2024-existing.pdf").exists())

    def test_post_missing_file_shows_form_error(self) -> None:
        """Posting without a file should re-render the form with an error."""
        response = self.client.post(self._import_url(), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")

    def test_post_unsupported_format_shows_error(self) -> None:
        upload = io.BytesIO(b"anything")
        upload.name = "archive.7z"
        response = self.client.post(self._import_url(), {"archive": upload}, follow=True)
        self.assertContains(response, "Unrecognised archive format")

    def test_post_zip_with_no_entries_shows_warning(self) -> None:
        zip_bytes = _make_zip_archive([])
        upload = io.BytesIO(zip_bytes)
        upload.name = "empty.zip"
        response = self.client.post(self._import_url(), {"archive": upload}, follow=True)
        self.assertContains(response, "No attachments could be extracted")

    # ------------------------------------------------------------------
    # Permission checks
    # ------------------------------------------------------------------

    def _limited_client(self, username: str, *permission_codenames: str) -> Client:
        """Return a Client logged in as a staff user with only the given permissions."""
        user = User.objects.create_user(username, f"{username}@b.com", "pass")
        user.is_staff = True
        user.save()
        for codename in permission_codenames:
            perm = Permission.objects.get(codename=codename)
            user.user_permissions.add(perm)
        client = Client()
        client.login(username=username, password="pass")
        return client

    def test_get_requires_receiveddocument_add_permission(self) -> None:
        """Staff user without add_receiveddocument permission should get 403."""
        client = self._limited_client("limited1", "add_attachment")
        response = client.get(self._import_url())
        self.assertEqual(response.status_code, 403)

    def test_get_requires_attachment_add_permission(self) -> None:
        """Staff user without add_attachment permission should get 403."""
        client = self._limited_client("limited2", "add_receiveddocument")
        response = client.get(self._import_url())
        self.assertEqual(response.status_code, 403)

    def test_get_allowed_with_both_permissions(self) -> None:
        """Staff with both add_receiveddocument and add_attachment can access."""
        client = self._limited_client("limited3", "add_receiveddocument", "add_attachment")
        response = client.get(self._import_url())
        self.assertEqual(response.status_code, 200)
