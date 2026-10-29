import base64
import json
from pathlib import Path

PDF_MAGIC = b"%PDF-"
FIDELITY_URL_PREFIX = "https://netbenefitsww.fidelity.com/mybenefitsww/spshistoryservices/activities/record/c:"

__all__ = [
    "PDF_MAGIC",
    "FIDELITY_URL_PREFIX",
    "UnsupportedArchiveError",
    "extract_fidelity_pdfs_from_har",
]


class UnsupportedArchiveError(Exception):
    """Raised when a HAR file is not a supported Fidelity archive."""


def _field(mapping: dict, key: str, kind: type) -> object:
    value = mapping.get(key)
    return value if isinstance(value, kind) else kind()


def extract_fidelity_pdfs_from_har(file_bytes: bytes) -> list[tuple[str, bytes]]:
    """Return ``(filename, pdf_bytes)`` tuples from a Fidelity HAR."""
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

        pdf_name = url[len(FIDELITY_URL_PREFIX) :].split("?")[0].split("#")[0]
        pdf_name = Path(pdf_name).name
        if not pdf_name:
            continue
        pdf_filename = pdf_name + ".pdf"

        content = _field(response, "content", dict)
        if "application/json" not in _field(content, "mimeType", str):
            continue

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
        except ValueError:
            continue

        if not pdf_bytes.startswith(PDF_MAGIC):
            continue

        results.append((pdf_filename, pdf_bytes))

    return results
