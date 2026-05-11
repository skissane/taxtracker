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
import json
from pathlib import Path

__all__ = [
    "UnsupportedArchiveError",
    "extract_from_archive",
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
# Public dispatcher
# ---------------------------------------------------------------------------


def extract_from_archive(file_bytes: bytes, filename: str) -> list[tuple[str, bytes]]:
    """Extract files from *file_bytes*, dispatching on *filename* extension.

    Returns a list of ``(output_filename, file_bytes)`` tuples.
    Raises ``UnsupportedArchiveError`` if the format is not recognised.
    """
    lower = filename.lower()
    if lower.endswith(".har"):
        return _extract_from_har(file_bytes)
    raise UnsupportedArchiveError(
        f"Unrecognised archive format for '{filename}'. Supported formats: .har"
    )
