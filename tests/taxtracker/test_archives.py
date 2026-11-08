import base64
import io
import json
import struct
import tracemalloc
import zipfile
from typing import Any
from unittest import mock

from django.test import TestCase

from lifetracker.taxtracker.archives import (
    UnsupportedArchiveError,
    extract_from_archive,
    extract_from_archive_with_skips,
)

# ---------------------------------------------------------------------------
# archives.py unit tests
# ---------------------------------------------------------------------------

PDF_MAGIC = b"%PDF-1.4 fake pdf content"
FIDELITY_URL_PREFIX = "https://netbenefitsww.fidelity.com/mybenefitsww/spshistoryservices/activities/record/c:"


def _make_fidelity_har(
    filename: str = "statement-2023-07-20",
    pdf_bytes: bytes | None = None,
    method: str = "GET",
    mime_type: str = "application/json",
    body_encoding: str | None = None,
) -> bytes:
    """Build minimal Fidelity HAR bytes for testing."""
    if pdf_bytes is None:
        pdf_bytes = PDF_MAGIC
    file_content_b64 = base64.b64encode(pdf_bytes).decode()
    body_json = json.dumps({"fileContent": file_content_b64})
    if body_encoding == "base64":
        body_text = base64.b64encode(body_json.encode()).decode()
    else:
        body_text = body_json

    entry: dict[str, Any] = {
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


def _make_zip_archive(entries: list[tuple[str, bytes]]) -> bytes:
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

    def test_har_extracts_pdf(self) -> None:
        har_bytes = _make_fidelity_har("statement-2023-07-20", PDF_MAGIC)
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(len(results), 1)
        name, data = results[0]
        self.assertEqual(name, "statement-2023-07-20.pdf")
        self.assertEqual(data, PDF_MAGIC)

    def test_har_base64_encoded_body(self) -> None:
        """HAR body itself Base64-encoded (body_encoding=base64)."""
        har_bytes = _make_fidelity_har("statement-2023-07-20", PDF_MAGIC, body_encoding="base64")
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], PDF_MAGIC)

    def test_har_filename_derived_from_url(self) -> None:
        har_bytes = _make_fidelity_har("mystatement", PDF_MAGIC)
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(results[0][0], "mystatement.pdf")

    def test_har_no_matching_entries(self) -> None:
        """A HAR with no Fidelity entries returns an empty list."""
        har: dict[str, object] = {"log": {"entries": []}}
        results = extract_from_archive(json.dumps(har).encode(), "empty.har")
        self.assertEqual(results, [])

    def test_har_skips_non_get_requests(self) -> None:
        har_bytes = _make_fidelity_har("statement-2023-07-20", PDF_MAGIC, method="POST")
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(results, [])

    def test_har_skips_wrong_mime_type(self) -> None:
        har_bytes = _make_fidelity_har("statement-2023-07-20", PDF_MAGIC, mime_type="text/html")
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(results, [])

    def test_har_skips_non_pdf_content(self) -> None:
        har_bytes = _make_fidelity_har("statement-2023-07-20", b"not a pdf")
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(results, [])

    def test_har_path_traversal_sanitized(self) -> None:
        """A URL suffix with path separators must not escape the output path."""
        har_bytes = _make_fidelity_har("../../../evil", PDF_MAGIC)
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(len(results), 1)
        name, _ = results[0]
        # basename only — no directory components
        self.assertEqual(name, "evil.pdf")
        self.assertNotIn("..", name)
        self.assertNotIn("/", name)

    def test_zip_extracts_pdf_names_and_normalises_uppercase_extension(self) -> None:
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

    def test_zip_reports_non_pdf_entries_as_skipped(self) -> None:
        zip_bytes = _make_zip_archive(
            [
                ("folder/statement.pdf", PDF_MAGIC),
                ("ignored.txt", b"text"),
            ]
        )
        _, skipped = extract_from_archive_with_skips(zip_bytes, "export.zip")
        self.assertEqual(skipped, ["ignored.txt"])

    def test_zip_skips_pdf_over_member_size_limit(self) -> None:
        zip_bytes = _make_zip_archive([("too-big.pdf", PDF_MAGIC + b"x" * 20)])
        with mock.patch("lifetracker.core.archives.MAX_ZIP_PDF_MEMBER_BYTES", 10):
            extracted, skipped = extract_from_archive_with_skips(zip_bytes, "export.zip")
        self.assertEqual(extracted, [])
        self.assertEqual(skipped, ["too-big.pdf"])

    def test_zip_skips_pdf_over_total_size_limit(self) -> None:
        first_pdf = PDF_MAGIC + b"x" * 5
        second_pdf = PDF_MAGIC + b"y" * 5
        zip_bytes = _make_zip_archive(
            [
                ("first.pdf", first_pdf),
                ("second.pdf", second_pdf),
            ]
        )
        with mock.patch("lifetracker.core.archives.MAX_ZIP_PDF_TOTAL_BYTES", len(first_pdf)):
            extracted, skipped = extract_from_archive_with_skips(zip_bytes, "export.zip")
        self.assertEqual([name for name, _ in extracted], ["first.pdf"])
        self.assertEqual(skipped, ["second.pdf"])

    def test_zip_skips_pdf_not_stored_or_deflated(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w") as zf:
            zf.writestr("deflated.pdf", PDF_MAGIC + b"a", compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("bzip2.pdf", PDF_MAGIC + b"b", compress_type=zipfile.ZIP_BZIP2)
            zf.writestr("lzma.pdf", PDF_MAGIC + b"c", compress_type=zipfile.ZIP_LZMA)
        extracted, skipped = extract_from_archive_with_skips(buf.getvalue(), "export.zip")
        self.assertEqual([name for name, _ in extracted], ["deflated.pdf"])
        self.assertEqual(skipped, ["bzip2.pdf", "lzma.pdf"])

    def test_zip_understated_member_size_does_not_exhaust_memory(self) -> None:
        # 32 MiB of zeros compresses to about 32 KiB; the headers then claim it's only 100 bytes.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            with zf.open("bomb.pdf", "w") as member:
                member.write(PDF_MAGIC)
                for _ in range(32):
                    member.write(bytes(1024 * 1024))
        zip_bytes = bytearray(buf.getvalue())
        struct.pack_into("<I", zip_bytes, zip_bytes.find(b"PK\x03\x04") + 22, 100)
        struct.pack_into("<I", zip_bytes, zip_bytes.find(b"PK\x01\x02") + 24, 100)
        tracemalloc.start()
        try:
            # Reading only the claimed 100 bytes fails the CRC check, so the entry is skipped.
            result = extract_from_archive_with_skips(bytes(zip_bytes), "export.zip")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result, ([], ["bomb.pdf"]))
        self.assertLess(peak, 4 * 1024 * 1024)

    def test_invalid_json_raises_unsupported(self) -> None:
        with self.assertRaises(UnsupportedArchiveError):
            extract_from_archive(b"not json at all", "export.har")

    def test_har_top_level_json_array_raises_unsupported(self) -> None:
        with self.assertRaisesMessage(UnsupportedArchiveError, "top-level JSON value must be an object"):
            extract_from_archive(json.dumps([]).encode(), "export.har")

    def test_har_entry_body_json_array_is_skipped(self) -> None:
        har_bytes = json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "method": "GET",
                                "url": f"{FIDELITY_URL_PREFIX}statement-2023-07-20",
                            },
                            "response": {
                                "content": {
                                    "mimeType": "application/json",
                                    "text": "[]",
                                }
                            },
                        }
                    ]
                }
            }
        ).encode()
        results = extract_from_archive(har_bytes, "export.har")
        self.assertEqual(results, [])

    def test_har_integer_over_digit_limit_raises_unsupported(self) -> None:
        with self.assertRaisesMessage(UnsupportedArchiveError, "Could not parse file as a HAR archive"):
            extract_from_archive(b'{"log": ' + b"1" * 5000 + b"}", "export.har")

    def test_har_wrong_shape_log_or_entries_raises_unsupported(self) -> None:
        for har, message in [
            ({"log": []}, "'log' must be an object"),
            ({"log": {"entries": {}}}, "'entries' must be a list"),
        ]:
            with self.subTest(har=har), self.assertRaisesMessage(UnsupportedArchiveError, message):
                extract_from_archive(json.dumps(har).encode(), "export.har")

    def test_har_skips_entries_with_wrong_field_types(self) -> None:
        """Entries whose fields have unexpected JSON types are skipped, not a 500."""
        good = json.loads(_make_fidelity_har("good"))["log"]["entries"][0]

        def variant(path: list[str], value: object) -> dict[str, Any]:
            entry: dict[str, Any] = json.loads(json.dumps(good))
            target = entry
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return entry

        entries = [
            "not an object",
            variant(["request"], []),
            variant(["response"], "x"),
            variant(["request", "method"], 1),
            variant(["request", "url"], None),
            variant(["response", "content"], []),
            variant(["response", "content", "mimeType"], 1),
            variant(["response", "content", "text"], {"fileContent": "x"}),
            variant(["response", "content", "text"], '{"fileContent": ' + "1" * 5000 + "}"),
            variant(["response", "content", "text"], json.dumps({"fileContent": 1})),
            variant(["response", "content", "text"], json.dumps({"fileContent": "\u00e9"})),
            good,
        ]
        har_bytes = json.dumps({"log": {"entries": entries}}).encode()
        self.assertEqual(extract_from_archive(har_bytes, "export.har"), [("good.pdf", PDF_MAGIC)])

    def test_invalid_zip_raises_unsupported(self) -> None:
        with self.assertRaisesMessage(UnsupportedArchiveError, "Could not parse file as a ZIP archive"):
            extract_from_archive(b"not a zip at all", "export.zip")

    # ------------------------------------------------------------------
    # Unsupported format
    # ------------------------------------------------------------------

    def test_unknown_extension_raises(self) -> None:
        with self.assertRaises(UnsupportedArchiveError):
            extract_from_archive(b"anything", "archive.7z")

    def test_no_extension_raises(self) -> None:
        with self.assertRaises(UnsupportedArchiveError):
            extract_from_archive(b"anything", "noextension")
