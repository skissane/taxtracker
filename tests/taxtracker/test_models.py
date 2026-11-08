import datetime
from unittest import mock

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from lifetracker.core.models import DBStoredFile, FileExtension, FileType, database_storage
from lifetracker.taxtracker.admin import FinancialYearAdmin
from lifetracker.taxtracker.financial_year import financial_year_for_date
from lifetracker.taxtracker.models import Attachment, FinancialYear, FinancialYearStatusHistory, Item, ReceivedDocument


class FinancialYearModelTests(TestCase):
    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)

    def test_str(self) -> None:
        self.assertEqual(str(self.fy), "FY2024")

    def test_start_date(self) -> None:
        self.assertEqual(self.fy.start_date, datetime.date(2023, 7, 1))

    def test_end_date(self) -> None:
        self.assertEqual(self.fy.end_date, datetime.date(2024, 6, 30))

    def test_financial_year_for_date_boundary(self) -> None:
        self.assertEqual(financial_year_for_date(datetime.date(2024, 6, 30)), 2024)
        self.assertEqual(financial_year_for_date(datetime.date(2024, 7, 1)), 2025)

    def test_start_and_end_dates_are_in_the_year(self) -> None:
        # start_date/end_date spell out the same 1 July boundary as financial_year_for_date.
        self.assertEqual(financial_year_for_date(self.fy.start_date), self.fy.year)
        self.assertEqual(financial_year_for_date(self.fy.end_date), self.fy.year)
        self.assertEqual(financial_year_for_date(self.fy.start_date - datetime.timedelta(days=1)), self.fy.year - 1)
        self.assertEqual(financial_year_for_date(self.fy.end_date + datetime.timedelta(days=1)), self.fy.year + 1)

    def test_default_lodgement_date(self) -> None:
        self.assertEqual(self.fy.default_lodgement_date, datetime.date(2024, 10, 31))

    def test_effective_lodgement_date_default(self) -> None:
        self.assertEqual(self.fy.effective_lodgement_date, datetime.date(2024, 10, 31))

    def test_effective_lodgement_date_override(self) -> None:
        override = datetime.date(2024, 12, 31)
        self.fy.lodgement_date_override = override
        self.fy.save()
        self.assertEqual(self.fy.effective_lodgement_date, override)

    def test_days_until_lodgement(self) -> None:
        today = timezone.localdate()
        expected = (self.fy.effective_lodgement_date - today).days
        self.assertEqual(self.fy.days_until_lodgement, expected)

    def test_days_until_lodgement_uses_local_date(self) -> None:
        self.fy.lodgement_date_override = datetime.date(2024, 10, 31)
        self.fy.save()
        with timezone.override("Australia/Sydney"):
            with mock.patch(
                "django.utils.timezone.now",
                return_value=datetime.datetime(2024, 10, 30, 14, 30, tzinfo=datetime.UTC),
            ):
                self.assertEqual(self.fy.days_until_lodgement, 0)

    def test_status_since_display_uses_local_date(self) -> None:
        self.fy.status = FinancialYear.STATUS_SUBMITTED
        self.fy.save()
        history = self.fy.status_history.latest("transitioned_at", "pk")
        history.transitioned_at = datetime.datetime(2024, 10, 30, 14, 30, tzinfo=datetime.UTC)
        history.save(update_fields=["transitioned_at"])

        with timezone.override("Australia/Sydney"):
            self.assertEqual(
                FinancialYearAdmin(FinancialYear, admin.site).status_since_display(self.fy),
                datetime.date(2024, 10, 31),
            )

    def test_default_status_is_pending_submission(self) -> None:
        self.assertEqual(self.fy.status, FinancialYear.STATUS_PENDING_SUBMISSION)

    def test_create_adds_initial_status_history(self) -> None:
        history = FinancialYearStatusHistory.objects.filter(financial_year=self.fy)
        self.assertEqual(history.count(), 1)
        self.assertIsNone(history.get().from_status)
        self.assertEqual(
            history.get().to_status,
            FinancialYear.STATUS_PENDING_SUBMISSION,
        )

    def test_status_change_adds_history(self) -> None:
        self.fy.status = FinancialYear.STATUS_SUBMITTED
        self.fy.save()

        latest = self.fy.status_history.latest("transitioned_at", "pk")
        self.assertEqual(latest.from_status, FinancialYear.STATUS_PENDING_SUBMISSION)
        self.assertEqual(latest.to_status, FinancialYear.STATUS_SUBMITTED)

    def test_status_change_can_be_reversed(self) -> None:
        self.fy.status = FinancialYear.STATUS_SUBMITTED
        self.fy.save()
        self.fy.status = FinancialYear.STATUS_MORE_INFO_REQUESTED
        self.fy.save()
        self.fy.status = FinancialYear.STATUS_SUBMITTED
        self.fy.save()

        latest = self.fy.status_history.latest("transitioned_at", "pk")
        self.assertEqual(latest.from_status, FinancialYear.STATUS_MORE_INFO_REQUESTED)
        self.assertEqual(latest.to_status, FinancialYear.STATUS_SUBMITTED)

    def test_status_change_without_loaded_status_reads_previous_from_db(self) -> None:
        # from_db() and save() always set _loaded_status, so this is save()'s
        # fallback for an instance that somehow lacks it.
        del self.fy._loaded_status
        self.fy.status = FinancialYear.STATUS_SUBMITTED
        self.fy.save()

        latest = self.fy.status_history.latest("transitioned_at", "pk")
        self.assertEqual(latest.from_status, FinancialYear.STATUS_PENDING_SUBMISSION)
        self.assertEqual(latest.to_status, FinancialYear.STATUS_SUBMITTED)

    def test_unchanged_status_without_loaded_status_adds_no_history(self) -> None:
        del self.fy._loaded_status
        self.fy.save()

        self.assertEqual(self.fy.status_history.count(), 1)

    def test_refresh_from_db_resets_loaded_status(self) -> None:
        fy = FinancialYear.objects.get(pk=self.fy.pk)
        FinancialYear.objects.filter(pk=fy.pk).update(status=FinancialYear.STATUS_SUBMITTED)

        fy.refresh_from_db()
        self.assertEqual(fy._loaded_status, FinancialYear.STATUS_SUBMITTED)
        fy.save()

        self.assertEqual(fy.status_history.count(), 1)

    def test_refresh_from_db_of_status_only_resets_loaded_status(self) -> None:
        fy = FinancialYear.objects.get(pk=self.fy.pk)
        FinancialYear.objects.filter(pk=fy.pk).update(status=FinancialYear.STATUS_SUBMITTED)

        # A generator, which Django's refresh_from_db() consumes.
        fy.refresh_from_db(fields=(name for name in ["status"]))
        self.assertEqual(fy._loaded_status, FinancialYear.STATUS_SUBMITTED)
        fy.save()

        self.assertEqual(fy.status_history.count(), 1)

    def test_refresh_from_db_of_other_fields_keeps_loaded_status(self) -> None:
        fy = FinancialYear.objects.get(pk=self.fy.pk)
        fy.status = FinancialYear.STATUS_SUBMITTED

        fy.refresh_from_db(fields=["lodgement_date_override"])
        self.assertEqual(fy._loaded_status, FinancialYear.STATUS_PENDING_SUBMISSION)
        fy.save()

        latest = fy.status_history.latest("transitioned_at", "pk")
        self.assertEqual(latest.from_status, FinancialYear.STATUS_PENDING_SUBMISSION)
        self.assertEqual(latest.to_status, FinancialYear.STATUS_SUBMITTED)

    def test_deferred_status_is_not_loaded(self) -> None:
        with self.assertNumQueries(1):
            fy = FinancialYear.objects.only("year").get(pk=self.fy.pk)
        self.assertIn("status", fy.get_deferred_fields())

        fy.status = FinancialYear.STATUS_SUBMITTED
        fy.save()

        latest = fy.status_history.latest("transitioned_at", "pk")
        self.assertEqual(latest.from_status, FinancialYear.STATUS_PENDING_SUBMISSION)
        self.assertEqual(latest.to_status, FinancialYear.STATUS_SUBMITTED)

    def test_ordering(self) -> None:
        fy2025 = FinancialYear.objects.create(year=2025)
        fy2023 = FinancialYear.objects.create(year=2023)
        years = list(FinancialYear.objects.values_list("year", flat=True))
        # Default ordering is descending by year.
        self.assertEqual(years[0], 2025)
        self.assertIn(2024, years)
        self.assertIn(2023, years)
        _ = fy2025, fy2023  # satisfy linter


class ItemModelTests(TestCase):
    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)
        self.parent = Item.objects.create(year=self.fy, title="Income", order=1)
        self.child = Item.objects.create(year=self.fy, parent=self.parent, title="Salary", order=1)

    def test_str_root(self) -> None:
        self.assertEqual(str(self.parent), "FY2024 > Income")

    def test_str_child(self) -> None:
        self.assertEqual(str(self.child), "FY2024 > Income > Salary")

    def test_is_done_false(self) -> None:
        self.assertFalse(self.parent.is_done)

    def test_is_done_true(self) -> None:
        self.parent.status = Item.STATUS_DONE
        self.assertTrue(self.parent.is_done)

    def test_get_folder_path_root(self) -> None:
        self.assertEqual(self.parent.get_folder_path(), ["Income"])

    def test_get_folder_path_child(self) -> None:
        self.assertEqual(self.child.get_folder_path(), ["Income", "Salary"])


class ItemYearInheritanceTests(TestCase):
    """Model-level enforcement: child items always inherit parent's year."""

    def setUp(self) -> None:
        self.fy2024 = FinancialYear.objects.create(year=2024)
        self.fy2025 = FinancialYear.objects.create(year=2025)
        self.parent = Item.objects.create(year=self.fy2024, title="Root", order=1)

    def test_save_auto_corrects_child_year_mismatch(self) -> None:
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

    def test_clean_auto_corrects_child_year_mismatch(self) -> None:
        """clean() silently corrects a mismatched year to match the parent."""
        child = Item(
            year=self.fy2025,  # wrong year
            parent=self.parent,
            title="Child",
            order=1,
        )
        child.clean()
        self.assertEqual(child.year_id, self.fy2024.pk)

    def test_save_with_correct_year_unchanged(self) -> None:
        """save() does not change year when child already has the correct year."""
        child = Item.objects.create(year=self.fy2024, parent=self.parent, title="Child", order=1)
        child.refresh_from_db()
        self.assertEqual(child.year_id, self.fy2024.pk)

    def test_root_item_year_not_affected(self) -> None:
        """Items without a parent keep whatever year is assigned."""
        root = Item.objects.create(year=self.fy2025, title="Root2", order=2)
        root.refresh_from_db()
        self.assertEqual(root.year_id, self.fy2025.pk)

    def test_direct_orm_save_corrects_year(self) -> None:
        """Direct ORM save (bypassing admin) still corrects the year."""
        child = Item.objects.create(year=self.fy2024, parent=self.parent, title="Child", order=1)
        # Directly change year via ORM (simulating a script/fixture misuse)
        child.year = self.fy2025
        child.save()
        child.refresh_from_db()
        self.assertEqual(child.year_id, self.fy2024.pk)

    def test_reparent_to_other_year_cascades_descendants(self) -> None:
        parent_2025 = Item.objects.create(
            year=self.fy2025,
            title="Parent 2025",
            order=2,
        )
        child = Item.objects.create(year=self.fy2024, parent=self.parent, title="Child", order=1)
        grandchild = Item.objects.create(year=self.fy2024, parent=child, title="Grandchild", order=1)

        child.parent = parent_2025
        child.save()

        child.refresh_from_db()
        grandchild.refresh_from_db()
        self.assertEqual(child.year_id, self.fy2025.pk)
        self.assertEqual(grandchild.year_id, self.fy2025.pk)

    def test_root_year_change_cascades_descendants_on_model_save(self) -> None:
        child = Item.objects.create(year=self.fy2024, parent=self.parent, title="Child", order=1)
        grandchild = Item.objects.create(year=self.fy2024, parent=child, title="Grandchild", order=1)

        self.parent.year = self.fy2025
        self.parent.save()

        child.refresh_from_db()
        grandchild.refresh_from_db()
        self.assertEqual(child.year_id, self.fy2025.pk)
        self.assertEqual(grandchild.year_id, self.fy2025.pk)

    def test_reparent_within_same_year_leaves_descendants(self) -> None:
        other_parent = Item.objects.create(year=self.fy2024, title="Other Parent 2024", order=2)
        child = Item.objects.create(year=self.fy2024, parent=self.parent, title="Child", order=1)
        grandchild = Item.objects.create(year=self.fy2024, parent=child, title="Grandchild", order=1)

        child.parent = other_parent
        with self.assertNumQueries(3):
            # Stored year, parent's year, then the UPDATE: no descendant walk.
            child.save()

        grandchild.refresh_from_db()
        self.assertEqual(grandchild.year_id, self.fy2024.pk)


class ReceivedDocumentModelTests(TestCase):
    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)

    def test_str(self) -> None:
        doc = ReceivedDocument.objects.create(year=self.fy, title="Notice of Assessment")
        self.assertEqual(str(doc), "FY2024: Notice of Assessment")

    def test_date_nullable(self) -> None:
        doc = ReceivedDocument.objects.create(year=self.fy, title="NOA")
        doc.refresh_from_db()
        self.assertIsNone(doc.date)

    def test_ordering(self) -> None:
        earlier = ReceivedDocument.objects.create(year=self.fy, title="Earlier", date=datetime.date(2024, 1, 1))
        later = ReceivedDocument.objects.create(year=self.fy, title="Later", date=datetime.date(2024, 6, 1))
        docs = list(ReceivedDocument.objects.all())
        # Default ordering is descending by date.
        self.assertEqual(docs, [later, earlier])


class ItemParentCycleTests(TestCase):
    """Tests that circular parent references are rejected with a ValidationError."""

    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)
        self.user = User.objects.create_superuser("admin", "admin@example.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")

    def test_direct_cycle_raises_validation_error(self) -> None:
        """Item cannot be its own parent (A → A)."""
        item = Item.objects.create(year=self.fy, title="A", order=1)
        item.parent_id = item.pk
        with self.assertRaises(ValidationError):
            item.clean()

    def test_indirect_two_element_cycle_raises_validation_error(self) -> None:
        """Two-element cycle: A → B, then B → A should be rejected."""
        b = Item.objects.create(year=self.fy, title="B", order=2)
        a = Item.objects.create(year=self.fy, parent=b, title="A", order=1)
        b.parent_id = a.pk
        with self.assertRaises(ValidationError):
            b.clean()

    def test_indirect_three_element_cycle_raises_validation_error(self) -> None:
        """Three-element cycle: A → B → C, then C → A should be rejected."""
        c = Item.objects.create(year=self.fy, title="C", order=3)
        b = Item.objects.create(year=self.fy, parent=c, title="B", order=2)
        a = Item.objects.create(year=self.fy, parent=b, title="A", order=1)
        c.parent_id = a.pk
        with self.assertRaises(ValidationError):
            c.clean()

    def _item_change_url(self, item: Item) -> str:
        return reverse("admin:taxtracker_item_change", args=[item.pk])

    def test_admin_form_rejects_direct_cycle(self) -> None:
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

    def test_admin_form_rejects_indirect_three_element_cycle(self) -> None:
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

    def test_str_is_cycle_safe_for_existing_cycle_in_db(self) -> None:
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

    def test_existing_cycle_does_not_block_resave(self) -> None:
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
            self.fail("clean() raised ValidationError for an unchanged pre-existing cycle")

    def test_admin_can_break_cycle_by_setting_parent_to_none(self) -> None:
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


class AttachmentTitleTests(TestCase):
    """Tests for Attachment.title auto-population and FK file_type."""

    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Income", order=1)
        self.ft = FileType.objects.create(short_name="PDF", full_name="PDF Document")

    def _make_simple_file(self, name: str = "document.pdf") -> ContentFile[bytes]:
        return ContentFile(b"%PDF-1.4 test", name=name)

    def test_title_auto_populated_from_filename(self) -> None:
        att = Attachment(item=self.item, file=self._make_simple_file("report.pdf"))
        att.save()
        self.assertEqual(att.title, "report.pdf")

    def test_explicit_title_not_overwritten(self) -> None:
        att = Attachment(
            item=self.item,
            title="My Report",
            file=self._make_simple_file("report.pdf"),
        )
        att.save()
        self.assertEqual(att.title, "My Report")

    def test_filetype_fk_saves_and_loads(self) -> None:
        att = Attachment(
            item=self.item,
            title="My Report",
            file_type=self.ft,
            file=self._make_simple_file("report.pdf"),
        )
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.file_type, self.ft)

    def test_filetype_nullable(self) -> None:
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

    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Income", order=1)

    def _make_file(self, name: str) -> ContentFile[bytes]:
        return ContentFile(b"data", name=name)

    # ------------------------------------------------------------------
    # Integration tests via Attachment.save()
    # ------------------------------------------------------------------

    def test_date_auto_extracted_from_filename(self) -> None:
        att = Attachment(item=self.item, file=self._make_file("statement-2026-03-15.pdf"))
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.date, datetime.date(2026, 3, 15))

    def test_explicit_date_not_overwritten(self) -> None:
        explicit = datetime.date(2000, 1, 1)
        att = Attachment(
            item=self.item,
            date=explicit,
            file=self._make_file("statement-2026-03-15.pdf"),
        )
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.date, explicit)

    def test_date_none_when_not_in_filename(self) -> None:
        att = Attachment(item=self.item, file=self._make_file("nodatehere.pdf"))
        att.save()
        att.refresh_from_db()
        self.assertIsNone(att.date)

    def test_date_none_when_not_supplied_and_not_extractable(self) -> None:
        att = Attachment(item=self.item, file=self._make_file("report.pdf"))
        att.save()
        att.refresh_from_db()
        self.assertIsNone(att.date)


class AttachmentParentValidationTests(TestCase):
    """Tests for Attachment's exactly-one-of item/received_document invariant."""

    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Income", order=1)
        self.doc = ReceivedDocument.objects.create(year=self.fy, title="NOA")

    def _make_file(self, name: str = "report.pdf") -> ContentFile[bytes]:
        return ContentFile(b"data", name=name)

    def test_item_only_is_valid(self) -> None:
        att = Attachment(item=self.item, file=self._make_file())
        att.full_clean()

    def test_received_document_only_is_valid(self) -> None:
        att = Attachment(received_document=self.doc, file=self._make_file())
        att.full_clean()

    def test_neither_parent_raises_validation_error(self) -> None:
        att = Attachment(file=self._make_file())
        with self.assertRaises(ValidationError):
            att.full_clean()

    def test_both_parents_raises_validation_error(self) -> None:
        att = Attachment(item=self.item, received_document=self.doc, file=self._make_file())
        with self.assertRaises(ValidationError):
            att.full_clean()

    def test_str_uses_item_when_item_is_parent(self) -> None:
        att = Attachment(item=self.item, title="Report", file=self._make_file())
        self.assertIn(str(self.item), str(att))

    def test_str_uses_received_document_when_received_document_is_parent(self) -> None:
        att = Attachment(received_document=self.doc, title="Report", file=self._make_file())
        self.assertIn(str(self.doc), str(att))

    def test_unsaved_parent_with_no_id_yet_is_valid(self) -> None:
        """clean() must not reject a child of a brand-new, not-yet-saved
        parent -- the FK id is only populated once the parent is actually
        saved (e.g. admin "add" view with an inline attachment). Calling
        clean() directly here (not full_clean()) isolates this from the
        separate, unrelated CheckConstraint pre-validation that Django's
        full_clean() also runs -- that check legitimately can't pass for
        an unsaved parent, but the real admin inline-formset flow never
        exercises it (the sibling parent field is absent from the child
        form there, so Django excludes it from validation)."""
        new_doc = ReceivedDocument(year=self.fy, title="Unsaved NOA")
        att = Attachment(received_document=new_doc, file=self._make_file())
        att.clean()


class AttachmentParentConstraintTests(TestCase):
    """The DB CheckConstraint fires even when clean()/full_clean() is bypassed."""

    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)

    def test_neither_parent_raises_integrity_error(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            Attachment.objects.create(title="Orphan", file=ContentFile(b"data", name="orphan.pdf"))

    def test_both_parents_raises_integrity_error(self) -> None:
        item = Item.objects.create(year=self.fy, title="Income", order=1)
        doc = ReceivedDocument.objects.create(year=self.fy, title="NOA")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Attachment.objects.create(
                item=item,
                received_document=doc,
                title="Both",
                file=ContentFile(b"data", name="both.pdf"),
            )


class DatabaseStorageTests(TestCase):
    """Tests for DatabaseStorage backend and file-storage related behaviours."""

    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Income", order=1)
        self.ft = FileType.objects.create(short_name="PDF", full_name="PDF Document")
        FileExtension.objects.create(file_type=self.ft, extension="pdf", is_primary=True)

    def _simple_file(self, name: str = "document.pdf", content: bytes = b"%PDF-1.4 test") -> ContentFile[bytes]:
        return ContentFile(content, name=name)

    # ------------------------------------------------------------------
    # File stored in database (not on disk)
    # ------------------------------------------------------------------

    def test_attachment_file_stored_in_db(self) -> None:
        """Uploading a file should create a DBStoredFile record."""
        before = DBStoredFile.objects.count()
        att = Attachment(item=self.item, file=self._simple_file("doc.pdf"))
        att.save()
        self.assertEqual(DBStoredFile.objects.count(), before + 1)

    def test_attachment_file_content_retrievable(self) -> None:
        """The file content saved to DB should match what was uploaded."""
        data = b"Hello database storage"
        att = Attachment(item=self.item, file=self._simple_file("hello.txt", data))
        att.save()
        att.refresh_from_db()
        stored_content = att.file.read()
        self.assertEqual(stored_content, data)

    def test_attachment_file_name_stored_correctly(self) -> None:
        """DBStoredFile.filename should be the uploaded filename."""
        att = Attachment(item=self.item, file=self._simple_file("myfile.pdf"))
        att.save()
        db_file = DBStoredFile.objects.get(pk=database_storage._name_to_pk(att.file.name))
        self.assertEqual(db_file.filename, "myfile.pdf")

    def test_attachment_file_deleted_removes_db_record(self) -> None:
        """Deleting an Attachment should remove the DBStoredFile record."""
        att = Attachment(item=self.item, file=self._simple_file("del.pdf"))
        att.save()
        pk = database_storage._name_to_pk(att.file.name)
        att.delete()
        self.assertFalse(DBStoredFile.objects.filter(pk=pk).exists())

    def test_cascade_deletes_remove_db_records(self) -> None:
        """Attachments deleted by cascade (which doesn't call their delete())
        still remove their DBStoredFile records."""
        doc = ReceivedDocument.objects.create(year=self.fy, title="Notice")
        child = Item.objects.create(year=self.fy, parent=self.item, title="Child", order=1)
        pks = []
        for kwargs in (
            {"item": self.item},
            {"item": child},
            {"received_document": doc},
        ):
            att = Attachment(file=self._simple_file("c.pdf"), **kwargs)
            att.save()
            pks.append(database_storage._name_to_pk(att.file.name))
        self.item.delete()
        self.assertFalse(DBStoredFile.objects.filter(pk__in=pks[:2]).exists())
        self.assertTrue(DBStoredFile.objects.filter(pk=pks[2]).exists())
        self.fy.delete()
        self.assertFalse(DBStoredFile.objects.filter(pk__in=pks).exists())

    def test_queryset_delete_removes_db_records(self) -> None:
        """QuerySet.delete() (as the admin's "Delete selected" action uses)
        removes the DBStoredFile records too."""
        att = Attachment(item=self.item, file=self._simple_file("q.pdf"))
        att.save()
        pk = database_storage._name_to_pk(att.file.name)
        Attachment.objects.filter(pk=att.pk).delete()
        self.assertFalse(DBStoredFile.objects.filter(pk=pk).exists())

    # ------------------------------------------------------------------
    # Auto-guess file type from extension
    # ------------------------------------------------------------------

    def test_auto_guess_file_type_from_extension(self) -> None:
        """file_type should be auto-set from the file extension when not provided."""
        att = Attachment(item=self.item, file=self._simple_file("report.pdf"))
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.file_type, self.ft)

    def test_auto_guess_file_type_case_insensitive(self) -> None:
        """Extension matching should be case-insensitive (e.g. .PDF → pdf)."""
        att = Attachment(item=self.item, file=self._simple_file("report.PDF"))
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.file_type, self.ft)

    def test_auto_guess_file_type_unknown_extension_leaves_null(self) -> None:
        """An unrecognised extension should leave file_type as None."""
        att = Attachment(item=self.item, file=self._simple_file("report.xyz"))
        att.save()
        att.refresh_from_db()
        self.assertIsNone(att.file_type)

    def test_explicit_file_type_not_overwritten(self) -> None:
        """An explicitly set file_type should not be overwritten by auto-guess."""
        ft2 = FileType.objects.create(short_name="TXT", full_name="Text File")
        att = Attachment(item=self.item, file=self._simple_file("report.pdf"), file_type=ft2)
        att.save()
        att.refresh_from_db()
        self.assertEqual(att.file_type, ft2)
