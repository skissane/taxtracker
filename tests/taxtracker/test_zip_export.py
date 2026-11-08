import datetime
import io
import zipfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import Client, TestCase
from django.urls import reverse

from lifetracker.core.models import DBStoredFile, FileType
from lifetracker.taxtracker.models import Attachment, FinancialYear, Item
from lifetracker.taxtracker.zip_export import _folder_path, adjust_notes_headings, build_fy_index_md

# ---------------------------------------------------------------------------
# adjust_notes_headings unit tests
# ---------------------------------------------------------------------------


class AdjustNotesHeadingsTests(TestCase):
    def test_no_headings_unchanged(self) -> None:
        notes = "Just some plain text.\nNo headings here."
        self.assertEqual(adjust_notes_headings(notes, 2), notes)

    def test_single_h1_becomes_h3(self) -> None:
        notes = "# My Heading\nSome text."
        result = adjust_notes_headings(notes, 2)
        self.assertIn("### My Heading", result)
        self.assertNotRegex(result, r"(?m)^# My Heading")

    def test_min_level_not_one(self) -> None:
        # Min is ##, so ## → ### and ### → ####
        notes = "## Section\n### Subsection"
        result = adjust_notes_headings(notes, 2)
        self.assertIn("### Section", result)
        self.assertIn("#### Subsection", result)

    def test_mixed_levels_scaled_correctly(self) -> None:
        notes = "# Top\n## Second\n### Third"
        result = adjust_notes_headings(notes, 2)
        self.assertIn("### Top", result)
        self.assertIn("#### Second", result)
        self.assertIn("##### Third", result)

    def test_heading_capped_at_h6(self) -> None:
        # item_heading_level=2 → target_base=3; min=1, so h5 → h5-1+3=h7 → capped h6
        notes = "# H1\n##### H5"
        result = adjust_notes_headings(notes, 2)
        self.assertIn("### H1", result)
        self.assertIn("###### H5", result)

    def test_body_text_preserved(self) -> None:
        notes = "# Heading\n\nSome **bold** text.\n\n- item"
        result = adjust_notes_headings(notes, 2)
        self.assertIn("Some **bold** text.", result)
        self.assertIn("- item", result)

    def test_headings_in_fenced_code_blocks_ignored(self) -> None:
        notes = '```bash\n# comment inside shell block\necho "hi"\n```\n## Real heading\n~~~\n# also ignored\n~~~\n'
        # The fenced "#" lines don't count, so "##" is the shallowest level.
        self.assertEqual(
            adjust_notes_headings(notes, 2),
            notes.replace("## Real heading", "### Real heading"),
        )

    def test_fence_closed_only_by_matching_fence(self) -> None:
        notes = (
            "````\n"
            "```\n"  # too short to close
            "~~~~\n"  # other character
            "```` not a close\n"  # text after it
            "# still code\n"
            "  ````\n"
            "# Heading\n"
        )
        self.assertEqual(
            adjust_notes_headings(notes, 2),
            notes.replace("# Heading", "### Heading"),
        )

    def test_unclosed_fence_runs_to_end(self) -> None:
        notes = "# Heading\n```\n# code\n"
        self.assertEqual(adjust_notes_headings(notes, 2), "### Heading\n```\n# code\n")

    def test_inline_code_span_does_not_open_fence(self) -> None:
        notes = "```inline code```\n# Heading\n"
        self.assertEqual(adjust_notes_headings(notes, 2), "```inline code```\n### Heading\n")

    def test_only_newline_splits_lines(self) -> None:
        notes = "# One\rtext\f# not a heading\n## Two"
        self.assertEqual(
            adjust_notes_headings(notes, 2),
            "### One\rtext\f# not a heading\n#### Two",
        )

    def test_line_endings_preserved(self) -> None:
        notes = "# One\r\n## Two\n# Three"
        self.assertEqual(adjust_notes_headings(notes, 2), "### One\r\n#### Two\n### Three")

    def test_hash_in_body_not_treated_as_heading(self) -> None:
        # Only lines where # is at the start are headings
        notes = "# Real heading\nThis has a # in the middle"
        result = adjust_notes_headings(notes, 2)
        self.assertIn("### Real heading", result)
        self.assertIn("This has a # in the middle", result)


class BuildFyIndexMdTests(TestCase):
    """build_fy_index_md() without a ZipFile, as the View Index action calls it."""

    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Income", order=1)

    def test_lists_attachment_type_path_and_notes(self) -> None:
        file_type = FileType.objects.create(short_name="PDF", full_name="PDF Document")
        Attachment.objects.create(
            item=self.item,
            title="Payslip",
            file=ContentFile(b"data", name="payslip.pdf"),
            file_type=file_type,
            notes="From employer",
        )
        index = build_fy_index_md(self.fy, prefix="FY2024/")
        self.assertIn(f"- **Payslip** ({file_type}) → `FY2024/Income/payslip.pdf`\n", index)
        self.assertIn("  From employer\n", index)

    def test_marks_attachment_with_missing_stored_file(self) -> None:
        attachment = Attachment.objects.create(
            item=self.item,
            title="Gone",
            file=ContentFile(b"data", name="gone.pdf"),
        )
        stored_pk = int((attachment.file.name or "").split("/")[1])
        DBStoredFile.objects.filter(pk=stored_pk).delete()
        index = build_fy_index_md(self.fy)
        self.assertIn("- **Gone** [MISSING]\n", index)

    def test_folder_path_of_child_item(self) -> None:
        child = Item.objects.create(year=self.fy, parent=self.item, title="Pay slips", order=1)
        self.assertEqual(_folder_path(child, {}), "Income/Pay_slips")


class ZipIndexHeadingAdjustmentTests(TestCase):
    """Integration: headings in item notes are re-levelled in the ZIP index."""

    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "admin@example.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")
        self.fy = FinancialYear.objects.create(year=2024)

    def _get_index(self) -> str:
        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        buf = io.BytesIO(response.getvalue())
        with zipfile.ZipFile(buf) as zf:
            return zf.read("index.md").decode()

    def test_h1_in_notes_becomes_h3_in_index(self) -> None:
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

    def test_h2_min_in_notes_becomes_h3_in_index(self) -> None:
        Item.objects.create(
            year=self.fy,
            title="Section",
            notes="## Overview\n### Detail",
            order=1,
        )
        index = self._get_index()
        self.assertIn("### Overview", index)
        self.assertIn("#### Detail", index)


class ZipDownloadTests(TestCase):
    """The single-year ZIP download's folder layout, names and index."""

    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client = Client()
        self.client.login(username="admin", password="password")
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Test Item", order=1)

    def test_download_zip_view_handles_parent_cycles(self) -> None:
        first = Item.objects.create(year=self.fy, title="Cycle A", order=9)
        second = Item.objects.create(year=self.fy, parent=first, title="Cycle B", order=1)
        first.parent = second
        first.save(update_fields=["parent"])
        Attachment.objects.create(
            item=first,
            title="cycle.pdf",
            file=ContentFile(b"cycle", name="cycle.pdf"),
        )

        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.getvalue())) as zf:
            self.assertTrue(any(name.endswith("/cycle.pdf") for name in zf.namelist()))

    def test_download_zip_spooled_to_disk(self) -> None:
        """A ZIP bigger than the spool limit is built on disk and still complete."""
        item = Item.objects.create(year=self.fy, title="Big", order=1)
        Attachment.objects.create(
            item=item,
            title="big.pdf",
            file=ContentFile(b"x" * 1000, name="big.pdf"),
        )
        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        with mock.patch("lifetracker.taxtracker.zip_export.ZIP_SPOOL_MAX_SIZE", 1):
            response = self.client.get(url)
        content = response.getvalue()
        self.assertEqual(int(response["Content-Length"]), len(content))
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            self.assertEqual(zf.read("Big/big.pdf"), b"x" * 1000)

    def test_download_zip_spaces_replaced_with_underscores(self) -> None:
        """Spaces in item titles/filenames must become underscores in ZIP paths."""
        item = Item.objects.create(year=self.fy, title="My Item With Spaces", order=2)
        Attachment.objects.create(
            item=item,
            title="my doc",
            file=ContentFile(b"data", name="my doc.pdf"),
        )
        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        buf = io.BytesIO(response.getvalue())
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

    def test_download_zip_notes_only_item_in_index(self) -> None:
        """Items with notes but no attachments must still appear in index.md."""
        Item.objects.create(
            year=self.fy,
            title="Notes Only Item",
            notes="This is an important note.",
            order=2,
        )
        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        buf = io.BytesIO(response.getvalue())
        with zipfile.ZipFile(buf) as zf:
            index_content = zf.read("index.md").decode()
        self.assertIn("Notes_Only_Item", index_content)
        self.assertIn("This is an important note.", index_content)

    def test_download_zip_index_sections_sorted_by_folder_path(self) -> None:
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

        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        buf = io.BytesIO(response.getvalue())
        with zipfile.ZipFile(buf) as zf:
            index_content = zf.read("index.md").decode()

        parent_heading = "## Brokerage_accounts\n"
        child_heading = "## Brokerage_accounts/AAA_Fidelity\n"
        zeta_heading = "## Zeta_top\n"
        self.assertLess(index_content.index(parent_heading), index_content.index(child_heading))
        self.assertLess(index_content.index(child_heading), index_content.index(zeta_heading))

    def test_download_zip_deduplicates_same_attachment_filename(self) -> None:
        item = Item.objects.create(year=self.fy, title="X", order=2)
        Attachment.objects.create(
            item=item,
            title="first",
            file=ContentFile(b"one", name="s.pdf"),
        )
        Attachment.objects.create(
            item=item,
            title="second",
            file=ContentFile(b"two", name="s.pdf"),
        )

        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        buf = io.BytesIO(response.getvalue())
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(zf.read("X/s.pdf"), b"one")
            self.assertEqual(zf.read("X/s (2).pdf"), b"two")
            index_content = zf.read("index.md").decode()
        self.assertIn("`X/s.pdf`", index_content)
        self.assertIn("`X/s (2).pdf`", index_content)

    def test_download_zip_suffixes_identical_attachments_by_pk(self) -> None:
        """Same date and title: the older attachment keeps the plain name."""
        item = Item.objects.create(year=self.fy, title="X", order=2)
        for content in (b"one", b"two"):
            Attachment.objects.create(
                item=item,
                title="same",
                date=datetime.date(2024, 1, 1),
                file=ContentFile(content, name="s.pdf"),
            )

        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        with zipfile.ZipFile(io.BytesIO(self.client.get(url).getvalue())) as zf:
            self.assertEqual(zf.read("X/s.pdf"), b"one")
            self.assertEqual(zf.read("X/s (2).pdf"), b"two")

    def test_download_zip_deduplicates_sanitized_sibling_titles(self) -> None:
        item1 = Item.objects.create(year=self.fy, title="A/B", order=2)
        item2 = Item.objects.create(year=self.fy, title="A_B", order=3)
        Attachment.objects.create(
            item=item1,
            title="first",
            file=ContentFile(b"one", name="doc.pdf"),
        )
        Attachment.objects.create(
            item=item2,
            title="second",
            file=ContentFile(b"two", name="doc.pdf"),
        )

        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        response = self.client.get(url)
        buf = io.BytesIO(response.getvalue())
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(zf.read("A_B/doc.pdf"), b"one")
            self.assertEqual(zf.read("A_B (2)/doc.pdf"), b"two")
            index_content = zf.read("index.md").decode()
        self.assertIn("## A_B\n", index_content)
        self.assertIn("## A_B (2)\n", index_content)
        self.assertIn("`A_B/doc.pdf`", index_content)
        self.assertIn("`A_B (2)/doc.pdf`", index_content)

    def test_download_zip_deduplicates_names_case_insensitively(self) -> None:
        """Names differing only in case would clash on macOS's filesystem."""
        upper = Item.objects.create(year=self.fy, title="Bank", order=2)
        lower = Item.objects.create(year=self.fy, title="bank", order=3)
        # A child folder and an attachment can't share a name either.
        Item.objects.create(year=self.fy, parent=upper, title="doc.pdf", order=1)
        for item, name in [
            (upper, "Statement.pdf"),
            (upper, "statement.pdf"),
            (upper, "doc.pdf"),
            (lower, "other.pdf"),
        ]:
            Attachment.objects.create(item=item, title=name, file=ContentFile(b"x", name=name))

        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        with zipfile.ZipFile(io.BytesIO(self.client.get(url).getvalue())) as zf:
            names = sorted(zf.namelist())
        self.assertEqual(
            names,
            [
                "Bank/Statement.pdf",
                "Bank/doc (2).pdf",
                "Bank/statement (2).pdf",
                "bank (2)/other.pdf",
                "index.md",
            ],
        )

    def test_download_zip_deduplicates_names_in_parent_cycles(self) -> None:
        """Two cycles with the same titles would otherwise share paths."""
        for content in (b"one", b"two"):
            first = Item.objects.create(year=self.fy, title="Cycle A", order=9)
            second = Item.objects.create(year=self.fy, parent=first, title="Cycle B", order=1)
            first.parent = second
            first.save(update_fields=["parent"])
            Attachment.objects.create(item=first, title="c", file=ContentFile(content, name="c.pdf"))

        url = reverse("admin:taxtracker_financialyear_download_zip", args=[self.fy.pk])
        with zipfile.ZipFile(io.BytesIO(self.client.get(url).getvalue())) as zf:
            pdfs = sorted(name for name in zf.namelist() if name.endswith(".pdf"))
            self.assertEqual(pdfs, ["…/Cycle_B/Cycle_A (2)/c.pdf", "…/Cycle_B/Cycle_A/c.pdf"])
            self.assertEqual({zf.read(name) for name in pdfs}, {b"one", b"two"})
