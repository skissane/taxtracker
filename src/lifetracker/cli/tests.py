import base64
import json
import tempfile
import zipfile
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from django.test import TestCase

from .extract_eml import (
    eml_to_zip,
    extract_eml_command,
    get_attachment_payload,
    get_email_date_prefix,
    get_unique_filename,
    iter_parts_with_date_prefix,
)
from .extract_fidelity_pdfs import (
    FIDELITY_URL_PREFIX,
    extract_fidelity_pdfs_command,
    extract_pdfs,
)
from .filter_zip import filter_zip, filter_zip_command
from .process_dates import (
    get_financial_year,
    group_by_financial_year,
    process_dates_command,
    read_dates,
    render_markdown,
)
from .zip_eml_to_pdf import convert_eml_zip_to_pdf, zip_eml_to_pdf_command
from .zip_eml_to_txt import convert_eml_zip_to_txt, zip_eml_to_txt_command

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
    outer.add_attachment(
        PDF_MAGIC, maintype="application", subtype="pdf", filename="doc.pdf"
    )
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
    nested.add_attachment(
        PDF_MAGIC, maintype="application", subtype="pdf", filename="payslip.pdf"
    )

    outer = EmailMessage()
    outer["From"] = "Sender <sender@example.com>"
    outer["To"] = "Recipient <recipient@example.com>"
    outer["Subject"] = "Test Email"
    outer["Date"] = "Mon, 01 Jul 2024 10:00:00 +0000"
    outer.set_content("Hello body")
    outer.add_attachment(nested, subtype="rfc822", filename="payslip.eml")
    return bytes(outer)


class ExtractEmlTests(TestCase):
    def test_iter_parts_with_date_prefix_payload_not_wrapped_in_list(self):
        nested = EmailMessage()
        nested["Date"] = "Tue, 02 Jul 2024 08:30:00 +0000"
        nested.set_content("body")
        part = EmailMessage()
        part["Content-Type"] = "message/rfc822"
        part.set_payload(nested)

        results = list(iter_parts_with_date_prefix(part))

        self.assertEqual([p for p, _ in results], [part, nested])
        self.assertEqual([prefix for _, prefix in results], ["2024-07-02-"] * 2)

    def test_nested_pdf_attachment_inherits_containing_email_date(self):
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

    def test_extracts_attachments_and_nested_email(self):
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

    def test_prefix_option_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            eml_path = Path(tmp) / "message.eml"
            eml_path.write_bytes(_build_eml_with_nested())
            zip_path = Path(tmp) / "out.zip"

            eml_to_zip(str(eml_path), str(zip_path), prefix="case1")

            with zipfile.ZipFile(zip_path) as zf:
                self.assertTrue(any(n.startswith("case1-doc") for n in zf.namelist()))

    def test_get_unique_filename_deduplicates(self):
        used: dict[str, int] = {}
        self.assertEqual(get_unique_filename("a.pdf", used), "a.pdf")
        self.assertEqual(get_unique_filename("a.pdf", used), "a_1.pdf")
        self.assertEqual(get_unique_filename("a.pdf", used), "a_2.pdf")

    def test_get_unique_filename_deduplicates_without_extension(self):
        used: dict[str, int] = {}
        self.assertEqual(get_unique_filename("noext", used), "noext")
        self.assertEqual(get_unique_filename("noext", used), "noext_1")

    def test_get_attachment_payload_message_payload_not_wrapped_in_list(self):
        nested = EmailMessage()
        nested.set_content("body")
        part = EmailMessage()
        part["Content-Type"] = "message/rfc822"
        # set_payload (unlike set_content) assigns the Message directly,
        # rather than wrapping it in a single-element list.
        part.set_payload(nested)
        self.assertIsNotNone(get_attachment_payload(part))

    def test_get_attachment_payload_message_unrecognised_payload(self):
        class FakePart:
            def get_content_type(self):
                return "message/rfc822"

            def get_payload(self):
                return "not a message object"

        self.assertIsNone(get_attachment_payload(FakePart()))

    def test_get_email_date_prefix_non_rfc822_part(self):
        part = EmailMessage()
        part.set_content("body")
        self.assertEqual(get_email_date_prefix(part), "")

    def test_get_email_date_prefix_payload_not_wrapped_in_list(self):
        nested = EmailMessage()
        nested["Date"] = "Tue, 02 Jul 2024 08:30:00 +0000"
        nested.set_content("body")
        part = EmailMessage()
        part["Content-Type"] = "message/rfc822"
        part.set_payload(nested)
        self.assertEqual(get_email_date_prefix(part), "2024-07-02-")

    def test_get_email_date_prefix_no_date_header(self):
        nested = EmailMessage()
        nested.set_content("body")
        part = EmailMessage()
        part["Content-Type"] = "message/rfc822"
        part.set_payload(nested)
        self.assertEqual(get_email_date_prefix(part), "")

    def test_get_email_date_prefix_unparseable_date(self):
        nested = EmailMessage()
        nested["Date"] = "not a real date"
        nested.set_content("body")
        part = EmailMessage()
        part["Content-Type"] = "message/rfc822"
        part.set_payload(nested)
        self.assertEqual(get_email_date_prefix(part), "")

    def test_get_email_date_prefix_unrecognised_payload(self):
        class FakePart:
            def get_content_type(self):
                return "message/rfc822"

            def get_payload(self):
                return object()

        self.assertEqual(get_email_date_prefix(FakePart()), "")

    def test_skips_attachment_with_no_extractable_payload(self):
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

    def test_cli_command_end_to_end(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            eml_path = Path(tmp) / "message.eml"
            eml_path.write_bytes(_build_eml_with_nested())
            zip_path = Path(tmp) / "out.zip"

            result = runner.invoke(extract_eml_command, [str(eml_path), str(zip_path)])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(zip_path.exists())
            self.assertIn("SUCCESS", result.output)


class ExtractFidelityPdfsTests(TestCase):
    @staticmethod
    def _make_har(
        filename="statement-2023-07-20",
        pdf_bytes=PDF_MAGIC,
        method="GET",
        mime_type="application/json",
        body_encoding=None,
        body_text=None,
        entries=None,
    ) -> bytes:
        if entries is None:
            if body_text is None:
                file_content_b64 = base64.b64encode(pdf_bytes).decode()
                body_json = json.dumps({"fileContent": file_content_b64})
                body_text = (
                    base64.b64encode(body_json.encode()).decode()
                    if body_encoding == "base64"
                    else body_json
                )
            entry = {
                "request": {
                    "method": method,
                    "url": f"{FIDELITY_URL_PREFIX}{filename}",
                },
                "response": {
                    "content": {"mimeType": mime_type, "text": body_text},
                },
            }
            if body_encoding:
                entry["response"]["content"]["encoding"] = body_encoding
            entries = [entry]
        return json.dumps({"log": {"entries": entries}}).encode()

    def test_extracts_matching_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har())
            out_dir = Path(tmp) / "out"

            count = extract_pdfs(har_path, out_dir)

            self.assertEqual(count, 1)
            self.assertEqual(
                (out_dir / "statement-2023-07-20.pdf").read_bytes(), PDF_MAGIC
            )

    def test_skips_non_get_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(method="POST"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_non_matching_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            entries = [
                {
                    "request": {"method": "GET", "url": "https://example.com/other"},
                    "response": {
                        "content": {"mimeType": "application/json", "text": "{}"}
                    },
                }
            ]
            har_path.write_bytes(self._make_har(entries=entries))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_wrong_mime_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(mime_type="text/html"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_non_json_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(body_text="not json"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_missing_file_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(body_text=json.dumps({})))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_bad_pdf_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(pdf_bytes=b"not a pdf"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_base64_encoded_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(body_encoding="base64"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 1)

    def test_path_traversal_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(filename="../../../evil"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 1)
            self.assertTrue((out_dir / "evil.pdf").exists())

    def test_skips_empty_filename(self):
        """A URL with nothing after the 'c:' prefix yields an empty basename."""
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(filename=""))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_undecodable_base64_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(
                self._make_har(body_encoding="base64", body_text="not-base64!!")
            )
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_empty_response_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(body_text=""))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_undecodable_base64_file_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(
                self._make_har(body_text=json.dumps({"fileContent": "abc"}))
            )
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_cli_missing_har_file_exits_with_error(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            result = runner.invoke(
                extract_fidelity_pdfs_command,
                [str(Path(tmp) / "missing.har"), str(Path(tmp) / "out")],
            )
            self.assertEqual(result.exit_code, 1)

    def test_cli_end_to_end(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har())
            out_dir = Path(tmp) / "out"

            result = runner.invoke(
                extract_fidelity_pdfs_command, [str(har_path), str(out_dir)]
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue((out_dir / "statement-2023-07-20.pdf").exists())

    def test_cli_reports_when_no_entries_match(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(json.dumps({"log": {"entries": []}}).encode())
            out_dir = Path(tmp) / "out"

            result = runner.invoke(
                extract_fidelity_pdfs_command,
                [str(har_path), str(out_dir)],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("No matching Fidelity PDF entries", result.output)


class FilterZipTests(TestCase):
    def test_filters_matching_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("keep.txt", b"keep")
                zf.writestr("reject_me.txt", b"reject")
            output_zip = Path(tmp) / "out.zip"

            filter_zip(str(input_zip), str(output_zip), "reject")

            with zipfile.ZipFile(output_zip) as zf:
                self.assertEqual(zf.namelist(), ["keep.txt"])
                self.assertEqual(zf.read("keep.txt"), b"keep")

    def test_missing_input_file_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                filter_zip(
                    str(Path(tmp) / "missing.zip"), str(Path(tmp) / "out.zip"), "x"
                )
            self.assertEqual(ctx.exception.code, 1)

    def test_bad_zip_file_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_zip = Path(tmp) / "bad.zip"
            bad_zip.write_bytes(b"not a zip")
            with self.assertRaises(SystemExit) as ctx:
                filter_zip(str(bad_zip), str(Path(tmp) / "out.zip"), "x")
            self.assertEqual(ctx.exception.code, 1)

    def test_cli_end_to_end(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("keep.txt", b"keep")
                zf.writestr("reject_me.txt", b"reject")
            output_zip = Path(tmp) / "out.zip"

            result = runner.invoke(
                filter_zip_command, [str(input_zip), str(output_zip), "reject"]
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("kept: 1", result.output)
            self.assertIn("skipped: 1", result.output)


class ProcessDatesTests(TestCase):
    def test_get_financial_year_boundary(self):
        import datetime

        self.assertEqual(get_financial_year(datetime.date(2024, 6, 30)), 2024)
        self.assertEqual(get_financial_year(datetime.date(2024, 7, 1)), 2025)

    def test_read_dates_parses_iso_and_ordinal_formats(self):
        dates = read_dates(
            ["2024-01-15\n", "November 20th 2024\n", "\n", "2024-01-15\n"], 0
        )
        import datetime

        self.assertEqual(
            dates, {datetime.date(2024, 1, 15), datetime.date(2024, 11, 20)}
        )

    def test_read_dates_applies_add_days(self):
        import datetime

        dates = read_dates(["2024-01-15\n"], 5)
        self.assertEqual(dates, {datetime.date(2024, 1, 20)})

    def test_read_dates_raises_on_bad_format(self):
        with self.assertRaises(ValueError) as ctx:
            read_dates(["not-a-date\n"], 0)
        self.assertIn("line 1", str(ctx.exception))

    def test_group_and_render_markdown(self):
        import datetime

        dates = {datetime.date(2024, 6, 30), datetime.date(2024, 7, 1)}
        groups = group_by_financial_year(dates)
        self.assertEqual(set(groups.keys()), {2024, 2025})

        rendered = render_markdown(groups, summary=False, year=None)
        self.assertIn("# FY2024 (1 date)", rendered)
        self.assertIn("# FY2025 (1 date)", rendered)
        self.assertIn("| 2024-06-30 | Sun |", rendered)

    def test_render_markdown_summary_only(self):
        import datetime

        groups = group_by_financial_year({datetime.date(2024, 1, 1)})
        rendered = render_markdown(groups, summary=True, year=None)
        self.assertEqual(rendered, "# FY2024 (1 date)")

    def test_render_markdown_year_filter(self):
        import datetime

        groups = group_by_financial_year(
            {datetime.date(2023, 1, 1), datetime.date(2024, 1, 1)}
        )
        rendered = render_markdown(groups, summary=True, year=2024)
        self.assertEqual(rendered, "# FY2024 (1 date)")

    def test_cli_end_to_end(self):
        runner = CliRunner()
        result = runner.invoke(
            process_dates_command, ["--summary"], input="2024-01-01\n2024-08-01\n"
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("FY2024", result.output)
        self.assertIn("FY2025", result.output)

    def test_cli_empty_input_produces_no_output(self):
        runner = CliRunner()
        result = runner.invoke(process_dates_command, [], input="")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "")

    def test_cli_bad_date_exits_with_error(self):
        runner = CliRunner()
        result = runner.invoke(process_dates_command, [], input="garbage\n")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("ERROR: Unexpected date format on line 1", result.output)


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


class ZipEmlToPdfTests(TestCase):
    def test_converts_eml_members_to_pdf(self):
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

    def test_no_eml_members_produces_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("ignored.txt", b"not an eml")
            output_zip = Path(tmp) / "out.zip"

            with patch("lifetracker.cli.zip_eml_to_pdf.pdfkit.from_string") as mock_fs:
                count = convert_eml_zip_to_pdf(str(input_zip), str(output_zip))

            self.assertEqual(count, 0)
            mock_fs.assert_not_called()

    def test_cli_end_to_end(self):
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
                result = runner.invoke(
                    zip_eml_to_pdf_command, [str(input_zip), str(output_zip)]
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Batch complete!", result.output)


class ZipEmlToTxtTests(TestCase):
    def test_extracts_plain_text_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("plain.eml", _build_plain_eml())
                zf.writestr("ignored.txt", b"not an eml")
            output_zip = Path(tmp) / "out.zip"

            count = convert_eml_zip_to_txt(str(input_zip), str(output_zip))

            self.assertEqual(count, 1)
            with zipfile.ZipFile(output_zip) as zf:
                self.assertEqual(zf.namelist(), ["plain.txt"])
                content = zf.read("plain.txt").decode()
                self.assertIn("Subject: Plain Email", content)
                self.assertIn("Plain body text", content)

    def test_skips_eml_without_plain_text_body(self):
        html_only = EmailMessage()
        html_only["Subject"] = "HTML Only"
        html_only["From"] = "sender@example.com"
        html_only["To"] = "recipient@example.com"
        html_only["Date"] = "Mon, 01 Jul 2024 10:00:00 +0000"
        html_only.set_content("<p>no plain part</p>", subtype="html")

        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("html_only.eml", bytes(html_only))
            output_zip = Path(tmp) / "out.zip"

            count = convert_eml_zip_to_txt(str(input_zip), str(output_zip))

            self.assertEqual(count, 0)
            with zipfile.ZipFile(output_zip) as zf:
                self.assertEqual(zf.namelist(), [])

    def test_no_eml_members_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("ignored.txt", b"not an eml")
            output_zip = Path(tmp) / "out.zip"

            self.assertIsNone(convert_eml_zip_to_txt(str(input_zip), str(output_zip)))

    def test_cli_end_to_end(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            input_zip = Path(tmp) / "in.zip"
            with zipfile.ZipFile(input_zip, "w") as zf:
                zf.writestr("plain.eml", _build_plain_eml())
            output_zip = Path(tmp) / "out.zip"

            result = runner.invoke(
                zip_eml_to_txt_command, [str(input_zip), str(output_zip)]
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Batch complete!", result.output)
