"""Archive processing helpers for the import-archive admin view.

Currently supports Fidelity HAR files.  The public API is intentionally
generic so that additional archive formats (ZIP, etc.) can be added later
without touching the view layer.

Public interface
----------------
``extract_from_archive(file_bytes, filename)``
    Dispatch to the appropriate extractor based on *filename*.
    Returns a list of ``(output_filename, pdf_bytes)`` tuples.
    Raises ``UnsupportedArchiveError`` when no extractor matches.

``UnsupportedArchiveError``
    Raised by ``extract_from_archive`` for unrecognised archive types.
"""

import base64
import binascii
import io
import json
import zipfile
from pathlib import Path

__all__ = [
    "UnsupportedArchiveError",
    "extract_from_archive",
    "extract_from_archive_with_skips",
]

PDF_MAGIC = b"%PDF-"
FIDELITY_URL_PREFIX = (
    "https://netbenefitsww.fidelity.com"
    "/mybenefitsww/spshistoryservices/activities/record/c:"
)


class UnsupportedArchiveError(Exception):
    """Raised when the uploaded file is not a recognised archive format."""


# ---------------------------------------------------------------------------
# Fidelity HAR extractor
# ---------------------------------------------------------------------------


def _extract_from_har(file_bytes: bytes) -> list[tuple[str, bytes]]:
    """Extract PDFs from a Fidelity HAR file.

    Returns a list of ``(filename, pdf_bytes)`` tuples, one per PDF found.
    Entries are silently skipped when any of the following conditions apply:
    - The request method is not GET.
    - The request URL does not match the Fidelity spshistoryservices prefix.
    - The response MIME type is not ``application/json``.
    - The response body is empty or cannot be decoded.
    - The ``fileContent`` key is absent from the JSON payload.
    - The ``fileContent`` value is not valid base64.
    - The decoded bytes do not start with the PDF magic bytes (``%PDF-``).
    """
    try:
        har = json.loads(file_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsupportedArchiveError(
            f"Could not parse file as a HAR archive: {exc}"
        ) from exc

    entries = har.get("log", {}).get("entries", [])
    results: list[tuple[str, bytes]] = []

    for entry in entries:
        request = entry.get("request", {})
        response = entry.get("response", {})

        if request.get("method", "").upper() != "GET":
            continue

        url: str = request.get("url", "")
        if not url.startswith(FIDELITY_URL_PREFIX):
            continue

        # Derive filename from the URL suffix after "/c:".
        pdf_name = url[len(FIDELITY_URL_PREFIX) :].split("?")[0].split("#")[0]
        # Sanitize: take only the basename to prevent path-traversal attacks.
        pdf_name = Path(pdf_name).name
        if not pdf_name:
            continue
        pdf_filename = pdf_name + ".pdf"

        # Require JSON content type.
        content = response.get("content", {})
        mime_type = content.get("mimeType", "")
        if "application/json" not in mime_type:
            continue

        # Decode the response body (may itself be Base64-encoded in the HAR).
        body_text = content.get("text", "")
        body_encoding = content.get("encoding", "")
        if body_encoding.lower() == "base64":
            try:
                body_str = base64.b64decode(body_text).decode("utf-8")
            except Exception:
                continue
        else:
            body_str = body_text

        if not body_str:
            continue

        try:
            payload = json.loads(body_str)
        except json.JSONDecodeError:
            continue

        file_content_b64 = payload.get("fileContent")
        if file_content_b64 is None:
            continue

        try:
            pdf_bytes = base64.b64decode(file_content_b64)
        except binascii.Error:
            continue

        if not pdf_bytes.startswith(PDF_MAGIC):
            continue

        results.append((pdf_filename, pdf_bytes))

    return results


# ---------------------------------------------------------------------------
# ZIP extractor
# ---------------------------------------------------------------------------


def _extract_from_zip(file_bytes: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Extract PDF files from a ZIP archive.

    Only entries ending with ``.pdf`` or ``.PDF`` are imported. Directory
    components are ignored and only the final path segment is used.
    Non-PDF entries are reported in the returned skipped list.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            results: list[tuple[str, bytes]] = []
            skipped: list[str] = []
            for info in archive.infolist():
                if info.is_dir():
                    continue
                entry_name = Path(info.filename.replace("\\", "/")).name
                if not entry_name:
                    continue
                if not (entry_name.endswith(".pdf") or entry_name.endswith(".PDF")):
                    skipped.append(entry_name)
                    continue
                if entry_name.endswith(".PDF"):
                    entry_name = entry_name[:-4] + ".pdf"
                pdf_bytes = archive.read(info)
                if not pdf_bytes.startswith(PDF_MAGIC):
                    skipped.append(entry_name)
                    continue
                results.append((entry_name, pdf_bytes))
            return results, skipped
    except zipfile.BadZipFile as exc:
        raise UnsupportedArchiveError(
            f"Could not parse file as a ZIP archive: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


def extract_from_archive(file_bytes: bytes, filename: str) -> list[tuple[str, bytes]]:
    """Extract files from *file_bytes*, dispatching on *filename* extension.

    Returns a list of ``(output_filename, file_bytes)`` tuples.
    Raises ``UnsupportedArchiveError`` if the format is not recognised.
    """
    extracted, _ = extract_from_archive_with_skips(file_bytes, filename)
    return extracted


def extract_from_archive_with_skips(
    file_bytes: bytes, filename: str
) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Extract files from *file_bytes* and return ``(extracted, skipped_names)``."""
    lower = filename.lower()
    if lower.endswith(".har"):
        return _extract_from_har(file_bytes), []
    if lower.endswith(".zip"):
        return _extract_from_zip(file_bytes)
    raise UnsupportedArchiveError(
        f"Unrecognised archive format for '{filename}'. Supported formats: .har, .zip"
    )
