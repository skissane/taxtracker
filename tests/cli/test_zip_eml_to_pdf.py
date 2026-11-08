import tempfile
import zipfile
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import click
from click.testing import CliRunner
from django.test import TestCase

from lifetracker.cli.zip_eml_to_pdf import convert_eml_zip_to_pdf, zip_eml_to_pdf_command


def _build_html_eml() -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "HTML Email"
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Date"] = "Mon, 01 Jul 2024 10:00:00 +0000"
    msg.set_content("plain fallback")
    msg.add_alternative("<p>Hello HTML</p>", subtype="html")
    return bytes(msg)


def _build_plain_eml() -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "Plain Email"
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Date"] = "Mon, 01 Jul 2024 10:00:00 +0000"
    msg.set_content("Plain body text")
    return bytes(msg)


def _build_eml_with_html_attachment() -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "Body plus attachment"
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Date"] = "Mon, 01 Jul 2024 10:00:00 +0000"
    msg.set_content("Body plain text")
    msg.add_attachment(b"<p>Attached HTML</p>", maintype="text", subtype="html", filename="attached.html")
    return bytes(msg)


class ZipEmlToPdfTests(TestCase):
    def test_converts_eml_members_to_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("html.eml", _build_html_eml())
                zf.writestr("plain.eml", _build_plain_eml())
                zf.writestr("ignored.txt", b"not an eml")
            output_zip = Path(tmp) / "out.zip"

            with patch(
                "lifetracker.cli.zip_eml_to_pdf.pdfkit.from_string",
                return_value=b"%PDF-FAKE",
            ) as mock_from_string:
                count = convert_eml_zip_to_pdf(str(input_zip), str(output_zip))

            self.assertEqual(count, 2)
            self.assertEqual(mock_from_string.call_count, 2)
            with zipfile.ZipFile(output_zip) as zf:
                self.assertEqual(sorted(zf.namelist()), ["html.pdf", "plain.pdf"])
                self.assertEqual(zf.read("html.pdf"), b"%PDF-FAKE")

            # The plain-text fallback path wraps the body in <pre> and escapes it.
            plain_html = mock_from_string.call_args_list[1].args[0]
            self.assertIn("<pre>", plain_html)

    def test_no_eml_members_raises_click_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("ignored.txt", b"not an eml")
            output_zip = Path(tmp) / "out.zip"
            output_zip.write_bytes(b"existing")

            with patch("lifetracker.cli.zip_eml_to_pdf.pdfkit.from_string") as mock_fs:
                with self.assertRaises(click.ClickException):
                    convert_eml_zip_to_pdf(str(input_zip), str(output_zip))

            mock_fs.assert_not_called()
            self.assertEqual(output_zip.read_bytes(), b"existing")

    def test_uses_message_body_not_html_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("mail.eml", _build_eml_with_html_attachment())
            output_zip = Path(tmp) / "out.zip"

            with patch(
                "lifetracker.cli.zip_eml_to_pdf.pdfkit.from_string",
                return_value=b"%PDF-FAKE",
            ) as mock_from_string:
                count = convert_eml_zip_to_pdf(str(input_zip), str(output_zip))

            self.assertEqual(count, 1)
            html = mock_from_string.call_args.args[0]
            self.assertIn("Body plain text", html)
            self.assertNotIn("Attached HTML", html)

    def test_message_without_text_body_gets_empty_body(self) -> None:
        msg = EmailMessage()
        msg["Subject"] = "Only an attachment"
        msg.set_content(b"%PDF-1.4", maintype="application", subtype="pdf")
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("mail.eml", msg.as_bytes())
            output_zip = Path(tmp) / "out.zip"

            with patch(
                "lifetracker.cli.zip_eml_to_pdf.pdfkit.from_string",
                return_value=b"%PDF-FAKE",
            ) as mock_from_string:
                count = convert_eml_zip_to_pdf(str(input_zip), str(output_zip))

            self.assertEqual(count, 1)
            self.assertIn("<pre></pre>", mock_from_string.call_args.args[0])

    def test_cli_end_to_end(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("html.eml", _build_html_eml())
            output_zip = Path(tmp) / "out.zip"

            with patch(
                "lifetracker.cli.zip_eml_to_pdf.pdfkit.from_string",
                return_value=b"%PDF-FAKE",
            ):
                result = runner.invoke(zip_eml_to_pdf_command, [str(input_zip), str(output_zip)])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Batch complete!", result.output)

    def test_cli_exits_non_zero_when_no_eml(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("ignored.txt", b"not an eml")
            output_zip = Path(tmp) / "out.zip"

            result = runner.invoke(zip_eml_to_pdf_command, [str(input_zip), str(output_zip)])

            self.assertEqual(result.exit_code, 1)
            self.assertIn("No .eml files found in the input ZIP.", result.output)
