"""Archive processing helpers for the import-archive admin view.

The import flow supports both Fidelity HAR files and `.zip` bundles of PDFs.
The public API is intentionally generic so additional formats can be added
without touching the view layer.

Public interface
----------------
``extract_from_archive(file_bytes, filename)``
    Dispatch to the appropriate extractor based on *filename*.
    Returns a list of ``(output_filename, pdf_bytes)`` tuples.
    Raises ``UnsupportedArchiveError`` when no extractor matches.

``extract_from_archive_with_skips(file_bytes, filename)``
    Like ``extract_from_archive``, but also returns the names of skipped ZIP entries
    (the HAR extractor skips entries silently, so reports none).

``UnsupportedArchiveError``
    Raised when an uploaded file is not a recognised archive format.
"""

import base64
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
FIDELITY_URL_PREFIX = "https://netbenefitsww.fidelity.com/mybenefitsww/spshistoryservices/activities/record/c:"
MAX_ZIP_PDF_MEMBER_BYTES = 20 * 1024 * 1024
MAX_ZIP_PDF_TOTAL_BYTES = 100 * 1024 * 1024


class UnsupportedArchiveError(Exception):
    """Raised when the uploaded file is not a recognised archive format."""


# ---------------------------------------------------------------------------
# Fidelity HAR extractor
# ---------------------------------------------------------------------------


def _field[T](mapping: dict, key: str, kind: type[T]) -> T:
    """Return ``mapping[key]`` if it's a *kind*, else an empty *kind* (``kind()``)."""
    value = mapping.get(key)
    return value if isinstance(value, kind) else kind()


def _extract_from_har(file_bytes: bytes) -> list[tuple[str, bytes]]:
    """Extract PDFs from a Fidelity HAR file.

    Returns a list of ``(filename, pdf_bytes)`` tuples, one per PDF found.
    Raises ``UnsupportedArchiveError`` if the file isn't JSON, or its top level,
    ``log`` or ``log.entries`` has the wrong type.
    Entries are silently skipped when any of the following conditions apply
    (a field of the wrong JSON type counts as missing):
    - The request method is not GET.
    - The request URL does not match the Fidelity spshistoryservices prefix.
    - The response MIME type is not ``application/json``.
    - The response body is empty or cannot be decoded, or isn't a JSON object.
    - The ``fileContent`` key is absent from the JSON payload.
    - The ``fileContent`` value is not valid base64.
    - The decoded bytes do not start with the PDF magic bytes (``%PDF-``).
    """
    try:
        har = json.loads(file_bytes.decode("utf-8"))
    except ValueError as exc:  # UnicodeDecodeError, JSONDecodeError, or an int over Python's digit limit
        raise UnsupportedArchiveError(f"Could not parse file as a HAR archive: {exc}") from exc
    if not isinstance(har, dict):
        raise UnsupportedArchiveError("Could not parse file as a HAR archive: top-level JSON value must be an object")
    log = har.get("log", {})
    if not isinstance(log, dict):
        raise UnsupportedArchiveError("Could not parse file as a HAR archive: 'log' must be an object")
    entries = log.get("entries", [])
    if not isinstance(entries, list):
        raise UnsupportedArchiveError("Could not parse file as a HAR archive: 'entries' must be a list")

    results: list[tuple[str, bytes]] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = _field(entry, "request", dict)
        response = _field(entry, "response", dict)

        if _field(request, "method", str).upper() != "GET":
            continue

        url = _field(request, "url", str)
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
        content = _field(response, "content", dict)
        if "application/json" not in _field(content, "mimeType", str):
            continue

        # Decode the response body (may itself be Base64-encoded in the HAR).
        body_text = _field(content, "text", str)
        if _field(content, "encoding", str).lower() == "base64":
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
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue

        file_content_b64 = _field(payload, "fileContent", str)
        if not file_content_b64:
            continue

        try:
            pdf_bytes = base64.b64decode(file_content_b64)
        except ValueError:  # binascii.Error, or a non-ASCII string
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
    Non-PDF entries and PDFs exceeding uncompressed size limits are reported in
    the returned skipped list.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            results: list[tuple[str, bytes]] = []
            skipped: list[str] = []
            total_pdf_size = 0
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
                if info.file_size > MAX_ZIP_PDF_MEMBER_BYTES:
                    skipped.append(entry_name)
                    continue
                if total_pdf_size + info.file_size > MAX_ZIP_PDF_TOTAL_BYTES:
                    skipped.append(entry_name)
                    continue
                # Not archive.read(): that asks the decompressor for up to 2 GiB at once and only then cuts the
                # result to file_size, so an entry whose header understates its size could still exhaust memory.
                with archive.open(info) as member:
                    pdf_bytes = member.read(info.file_size)
                if not pdf_bytes.startswith(PDF_MAGIC):
                    skipped.append(entry_name)
                    continue
                total_pdf_size += info.file_size
                results.append((entry_name, pdf_bytes))
            return results, skipped
    except zipfile.BadZipFile as exc:
        raise UnsupportedArchiveError(f"Could not parse file as a ZIP archive: {exc}") from exc


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


def extract_from_archive_with_skips(file_bytes: bytes, filename: str) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Extract files from *file_bytes* and return ``(extracted, skipped_names)``."""
    lower = filename.lower()
    if lower.endswith(".har"):
        return _extract_from_har(file_bytes), []
    if lower.endswith(".zip"):
        return _extract_from_zip(file_bytes)
    raise UnsupportedArchiveError(f"Unrecognised archive format for '{filename}'. Supported formats: .har, .zip")
