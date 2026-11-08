import tempfile
import typing
import zipfile
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from django.test import TestCase

from lifetracker.cli.extract_eml import (
    eml_to_zip,
    extract_eml_command,
    get_attachment_payload,
    get_email_date_prefix,
    get_unique_filename,
    iter_parts_with_date_prefix,
)

PDF_MAGIC = b"%PDF-1.4 fake"


def _build_eml_with_nested() -> bytes:
    """A multipart .eml with a plain-text body, a PDF attachment, and a
    nested message/rfc822 attachment (with no explicit filename, exercising
    the 'nested_email_N.eml' fallback name)."""
    nested = EmailMessage()
    nested["From"] = "Nested Sender <nested@example.com>"
    nested["To"] = "Nested Recipient <nr@example.com>"
    nested["Subject"] = "Nested Subject"
    nested["Date"] = "Tue, 02 Jul 2024 08:30:00 +0000"
    nested.set_content("Nested body")

    outer = EmailMessage()
    outer["From"] = "Sender <sender@example.com>"
    outer["To"] = "Recipient <recipient@example.com>"
    outer["Subject"] = "Test Email"
    outer["Date"] = "Mon, 01 Jul 2024 10:00:00 +0000"
    outer.set_content("Hello body")
    outer.add_attachment(PDF_MAGIC, maintype="application", subtype="pdf", filename="doc.pdf")
    outer.add_attachment(nested, subtype="rfc822")
    return bytes(outer)


def _build_eml_with_nested_pdf_attachment() -> bytes:
    """A nested message/rfc822 attachment that itself has a PDF attachment,
    exercising date-prefix inheritance for attachments nested inside a
    forwarded email."""
    nested = EmailMessage()
    nested["From"] = "Payroll <payroll@example.com>"
    nested["To"] = "Recipient <recipient@example.com>"
    nested["Subject"] = "Payslip"
    nested["Date"] = "Tue, 02 Jul 2024 08:30:00 +0000"
    nested.set_content("Payslip body")
    nested.add_attachment(PDF_MAGIC, maintype="application", subtype="pdf", filename="payslip.pdf")

    outer = EmailMessage()
    outer["From"] = "Sender <sender@example.com>"
    outer["To"] = "Recipient <recipient@example.com>"
    outer["Subject"] = "Test Email"
    outer["Date"] = "Mon, 01 Jul 2024 10:00:00 +0000"
    outer.set_content("Hello body")
    outer.add_attachment(nested, subtype="rfc822", filename="payslip.eml")
    return bytes(outer)


class ExtractEmlTests(TestCase):
    def test_iter_parts_with_date_prefix_payload_not_wrapped_in_list(self) -> None:
        nested = EmailMessage()
        nested["Date"] = "Tue, 02 Jul 2024 08:30:00 +0000"
        nested.set_content("body")
        part = EmailMessage()
        part["Content-Type"] = "message/rfc822"
        part.set_payload(nested)

        results = list(iter_parts_with_date_prefix(part))

        self.assertEqual([p for p, _ in results], [part, nested])
        self.assertEqual([prefix for _, prefix in results], ["2024-07-02-"] * 2)

    def test_nested_pdf_attachment_inherits_containing_email_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            eml_path = Path(tmp) / "message.eml"
            eml_path.write_bytes(_build_eml_with_nested_pdf_attachment())
            zip_path = Path(tmp) / "out.zip"

            eml_to_zip(str(eml_path), str(zip_path))

            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                self.assertIn("2024-07-02-payslip.eml", names)
                self.assertIn("2024-07-02-payslip.pdf", names)
                self.assertEqual(zf.read("2024-07-02-payslip.pdf"), PDF_MAGIC)

    def test_extracts_attachments_and_nested_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            eml_path = Path(tmp) / "message.eml"
            eml_path.write_bytes(_build_eml_with_nested())
            zip_path = Path(tmp) / "out.zip"

            eml_to_zip(str(eml_path), str(zip_path))

            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                self.assertIn("doc.pdf", names)
                self.assertEqual(zf.read("doc.pdf"), PDF_MAGIC)
                # Nested email has no explicit filename -> fallback name,
                # prefixed with its own Date header.
                nested_names = [n for n in names if "nested-email" in n]
                self.assertEqual(len(nested_names), 1)
                self.assertTrue(nested_names[0].startswith("2024-07-02-"))

    def test_prefix_option_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            eml_path = Path(tmp) / "message.eml"
            eml_path.write_bytes(_build_eml_with_nested())
            zip_path = Path(tmp) / "out.zip"

            eml_to_zip(str(eml_path), str(zip_path), prefix="case1")

            with zipfile.ZipFile(zip_path) as zf:
                self.assertTrue(any(n.startswith("case1-doc") for n in zf.namelist()))

    def test_get_unique_filename_deduplicates(self) -> None:
        used: dict[str, int] = {}
        self.assertEqual(get_unique_filename("a.pdf", used), "a.pdf")
        self.assertEqual(get_unique_filename("a.pdf", used), "a_1.pdf")
        self.assertEqual(get_unique_filename("a.pdf", used), "a_2.pdf")

    def test_get_unique_filename_deduplicates_without_extension(self) -> None:
        used: dict[str, int] = {}
        self.assertEqual(get_unique_filename("noext", used), "noext")
        self.assertEqual(get_unique_filename("noext", used), "noext_1")

    def test_get_attachment_payload_message_payload_not_wrapped_in_list(self) -> None:
        nested = EmailMessage()
        nested.set_content("body")
        part = EmailMessage()
        part["Content-Type"] = "message/rfc822"
        # set_payload (unlike set_content) assigns the Message directly,
        # rather than wrapping it in a single-element list.
        part.set_payload(nested)
        self.assertIsNotNone(get_attachment_payload(part))

    def test_get_attachment_payload_message_unrecognised_payload(self) -> None:
        class FakePart:
            def get_content_type(self) -> str:
                return "message/rfc822"

            def get_payload(self) -> object:
                return "not a message object"

        self.assertIsNone(get_attachment_payload(typing.cast(EmailMessage, FakePart())))

    def test_get_email_date_prefix_non_rfc822_part(self) -> None:
        part = EmailMessage()
        part.set_content("body")
        self.assertEqual(get_email_date_prefix(part), "")

    def test_get_email_date_prefix_payload_not_wrapped_in_list(self) -> None:
        nested = EmailMessage()
        nested["Date"] = "Tue, 02 Jul 2024 08:30:00 +0000"
        nested.set_content("body")
        part = EmailMessage()
        part["Content-Type"] = "message/rfc822"
        part.set_payload(nested)
        self.assertEqual(get_email_date_prefix(part), "2024-07-02-")

    def test_get_email_date_prefix_no_date_header(self) -> None:
        nested = EmailMessage()
        nested.set_content("body")
        part = EmailMessage()
        part["Content-Type"] = "message/rfc822"
        part.set_payload(nested)
        self.assertEqual(get_email_date_prefix(part), "")

    def test_get_email_date_prefix_unparseable_date(self) -> None:
        nested = EmailMessage()
        nested["Date"] = "not a real date"
        nested.set_content("body")
        part = EmailMessage()
        part["Content-Type"] = "message/rfc822"
        part.set_payload(nested)
        self.assertEqual(get_email_date_prefix(part), "")

    def test_get_email_date_prefix_unrecognised_payload(self) -> None:
        class FakePart:
            def get_content_type(self) -> str:
                return "message/rfc822"

            def get_payload(self) -> object:
                return object()

        self.assertEqual(get_email_date_prefix(typing.cast(EmailMessage, FakePart())), "")

    def test_skips_attachment_with_no_extractable_payload(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "lifetracker.cli.extract_eml.get_attachment_payload",
                return_value=None,
            ),
        ):
            eml_path = Path(tmp) / "message.eml"
            eml_path.write_bytes(_build_eml_with_nested())
            zip_path = Path(tmp) / "out.zip"

            eml_to_zip(str(eml_path), str(zip_path))

            with zipfile.ZipFile(zip_path) as zf:
                self.assertEqual(zf.namelist(), [])

    def test_cli_command_end_to_end(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            eml_path = Path(tmp) / "message.eml"
            eml_path.write_bytes(_build_eml_with_nested())
            zip_path = Path(tmp) / "out.zip"

            result = runner.invoke(extract_eml_command, [str(eml_path), str(zip_path)])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(zip_path.exists())
            self.assertIn("SUCCESS", result.output)
