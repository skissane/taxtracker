"""Extract the PDFs embedded in a Fidelity NetBenefits HAR file.

The one implementation, shared by the archive import (`taxtracker/archives.py`) and the `extract-fidelity-pdfs`
command-line tool, so kept free of Django imports, as the command-line tools (`lifetracker.cli`) use it too.
"""

import base64
import json
from collections.abc import Callable
from pathlib import Path

PDF_MAGIC = b"%PDF-"
FIDELITY_URL_PREFIX = "https://netbenefitsww.fidelity.com/mybenefitsww/spshistoryservices/activities/record/c:"


class UnsupportedArchiveError(Exception):
    """Raised when the uploaded file is not a recognised archive format."""


def _field[T](mapping: dict, key: str, kind: type[T]) -> T:
    """Return ``mapping[key]`` if it's a *kind*, else an empty *kind* (``kind()``)."""
    value = mapping.get(key)
    return value if isinstance(value, kind) else kind()


def extract_pdfs_from_har(
    file_bytes: bytes, on_skip: Callable[[str, str], None] | None = None
) -> list[tuple[str, bytes]]:
    """Extract PDFs from a Fidelity HAR file.

    Returns a list of ``(filename, pdf_bytes)`` tuples, one per PDF found.
    Raises ``UnsupportedArchiveError`` if the file isn't JSON, or its top level,
    ``log`` or ``log.entries`` has the wrong type.
    Entries that aren't GET requests to the Fidelity spshistoryservices URL prefix are ignored.
    A matching entry is skipped, calling ``on_skip(url, reason)`` if given, when any of the following
    conditions apply (a field of the wrong JSON type counts as missing):
    - No filename is left after the URL prefix.
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

    def skip(url: str, reason: str) -> None:
        if on_skip is not None:
            on_skip(url, reason)

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
            skip(url, "could not determine filename from URL")
            continue
        pdf_filename = pdf_name + ".pdf"

        # Require JSON content type.
        content = _field(response, "content", dict)
        mime_type = _field(content, "mimeType", str)
        if "application/json" not in mime_type:
            skip(url, f"unexpected content type {mime_type!r}")
            continue

        # Decode the response body (may itself be Base64-encoded in the HAR).
        body_text = _field(content, "text", str)
        if _field(content, "encoding", str).lower() == "base64":
            try:
                body_str = base64.b64decode(body_text).decode("utf-8")
            except ValueError as exc:  # binascii.Error, UnicodeDecodeError, or a non-ASCII string
                skip(url, f"could not decode Base64-encoded body: {exc}")
                continue
        else:
            body_str = body_text

        if not body_str:
            skip(url, "empty response body")
            continue

        try:
            payload = json.loads(body_str)
        except ValueError as exc:
            skip(url, f"could not parse JSON response body: {exc}")
            continue
        if not isinstance(payload, dict):
            skip(url, "JSON response body is not an object")
            continue

        file_content_b64 = _field(payload, "fileContent", str)
        if not file_content_b64:
            skip(url, "'fileContent' key missing in response")
            continue

        try:
            pdf_bytes = base64.b64decode(file_content_b64)
        except ValueError as exc:  # binascii.Error, or a non-ASCII string
            skip(url, f"could not Base64-decode fileContent: {exc}")
            continue

        if not pdf_bytes.startswith(PDF_MAGIC):
            skip(url, f"decoded data does not look like a PDF (starts with {pdf_bytes[:8]!r})")
            continue

        results.append((pdf_filename, pdf_bytes))

    return results
