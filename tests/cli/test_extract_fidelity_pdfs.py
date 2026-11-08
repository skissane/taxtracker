import base64
import contextlib
import io
import json
import tempfile
from pathlib import Path
from typing import Any

import click
from click.testing import CliRunner
from django.test import TestCase

from lifetracker.cli.extract_fidelity_pdfs import extract_fidelity_pdfs_command, extract_pdfs
from lifetracker.taxtracker.fidelity_har import FIDELITY_URL_PREFIX
from tests.cli.test_extract_eml import PDF_MAGIC


class ExtractFidelityPdfsTests(TestCase):
    @staticmethod
    def _make_har(
        filename: str = "statement-2023-07-20",
        pdf_bytes: bytes = PDF_MAGIC,
        method: str = "GET",
        mime_type: str = "application/json",
        body_encoding: str | None = None,
        body_text: str | None = None,
        entries: list[dict[str, Any]] | None = None,
    ) -> bytes:
        if entries is None:
            if body_text is None:
                file_content_b64 = base64.b64encode(pdf_bytes).decode()
                body_json = json.dumps({"fileContent": file_content_b64})
                body_text = base64.b64encode(body_json.encode()).decode() if body_encoding == "base64" else body_json
            entry: dict[str, Any] = {
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

    def test_extracts_matching_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har())
            out_dir = Path(tmp) / "out"

            count = extract_pdfs(har_path, out_dir)

            self.assertEqual(count, 1)
            self.assertEqual((out_dir / "statement-2023-07-20.pdf").read_bytes(), PDF_MAGIC)

    def test_skips_non_get_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(method="POST"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_non_matching_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            entries = [
                {
                    "request": {"method": "GET", "url": "https://example.com/other"},
                    "response": {"content": {"mimeType": "application/json", "text": "{}"}},
                }
            ]
            har_path.write_bytes(self._make_har(entries=entries))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_wrong_mime_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(mime_type="text/html"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_non_json_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(body_text="not json"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_missing_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(body_text=json.dumps({})))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_bad_pdf_magic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(pdf_bytes=b"not a pdf"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_base64_encoded_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(body_encoding="base64"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 1)

    def test_path_traversal_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(filename="../../../evil"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 1)
            self.assertTrue((out_dir / "evil.pdf").exists())

    def test_skips_empty_filename(self) -> None:
        """A URL with nothing after the 'c:' prefix yields an empty basename."""
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(filename=""))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_undecodable_base64_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(body_encoding="base64", body_text="not-base64!!"))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_empty_response_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(body_text=""))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_skips_undecodable_base64_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(body_text=json.dumps({"fileContent": "abc"})))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 0)

    def test_cli_missing_har_file_exits_with_error(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            result = runner.invoke(
                extract_fidelity_pdfs_command,
                [str(Path(tmp) / "missing.har"), str(Path(tmp) / "out")],
            )
            self.assertEqual(result.exit_code, 1)

    def test_cli_end_to_end(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har())
            out_dir = Path(tmp) / "out"

            result = runner.invoke(extract_fidelity_pdfs_command, [str(har_path), str(out_dir)])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue((out_dir / "statement-2023-07-20.pdf").exists())

    def test_warns_about_skipped_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har(mime_type="text/html"))
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                self.assertEqual(extract_pdfs(har_path, Path(tmp) / "out"), 0)

            url = f"{FIDELITY_URL_PREFIX}statement-2023-07-20"
            self.assertEqual(stderr.getvalue(), f"WARNING: unexpected content type 'text/html' for {url}; skipping\n")

    def test_cli_invalid_har_exits_with_error(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(b"not json")

            result = runner.invoke(extract_fidelity_pdfs_command, [str(har_path), str(Path(tmp) / "out")])

            self.assertEqual(result.exit_code, 1)
            self.assertIn("Error: Could not parse file as a HAR archive", result.output)

    def test_cli_reports_when_no_entries_match(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(json.dumps({"log": {"entries": []}}).encode())
            out_dir = Path(tmp) / "out"

            result = runner.invoke(
                extract_fidelity_pdfs_command,
                [str(har_path), str(out_dir)],
            )

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("No matching Fidelity PDF entries", result.output)
            self.assertFalse(out_dir.exists())

    def test_refuses_to_overwrite_existing_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har())
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            (out_dir / "statement-2023-07-20.pdf").write_bytes(b"existing")

            with self.assertRaises(click.ClickException):
                extract_pdfs(har_path, out_dir)

            self.assertEqual((out_dir / "statement-2023-07-20.pdf").read_bytes(), b"existing")

    def test_refuses_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_har())
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            target = Path(tmp) / "elsewhere.pdf"
            (out_dir / "statement-2023-07-20.pdf").symlink_to(target)

            with self.assertRaises(click.ClickException):
                extract_pdfs(har_path, out_dir)

            self.assertFalse(target.exists())

    def _make_multi_har(self, *pdfs: tuple[str, bytes]) -> bytes:
        entries = []
        for filename, pdf_bytes in pdfs:
            entries += json.loads(self._make_har(filename=filename, pdf_bytes=pdf_bytes))["log"]["entries"]
        return self._make_har(entries=entries)

    def test_refuses_before_writing_anything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_multi_har(("a", PDF_MAGIC), ("b", PDF_MAGIC)))
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            (out_dir / "b.pdf").write_bytes(b"existing")

            with self.assertRaises(click.ClickException):
                extract_pdfs(har_path, out_dir)

            self.assertFalse((out_dir / "a.pdf").exists())

    def test_writes_one_copy_of_identical_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_multi_har(("a", PDF_MAGIC), ("a", PDF_MAGIC)))
            out_dir = Path(tmp) / "out"

            self.assertEqual(extract_pdfs(har_path, out_dir), 1)
            self.assertEqual((out_dir / "a.pdf").read_bytes(), PDF_MAGIC)

    def test_refuses_different_pdfs_with_the_same_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "export.har"
            har_path.write_bytes(self._make_multi_har(("a", PDF_MAGIC), ("a", PDF_MAGIC + b"2")))
            out_dir = Path(tmp) / "out"

            with self.assertRaisesRegex(click.ClickException, "different PDFs named a.pdf"):
                extract_pdfs(har_path, out_dir)

            self.assertFalse(out_dir.exists())
