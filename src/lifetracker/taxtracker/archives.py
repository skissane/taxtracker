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

import io
import zipfile
from pathlib import Path

from lifetracker.core.fidelity_har import (
    PDF_MAGIC,
    UnsupportedArchiveError,
    extract_fidelity_pdfs_from_har,
)

__all__ = [
    "UnsupportedArchiveError",
    "extract_from_archive",
    "extract_from_archive_with_skips",
]


# ---------------------------------------------------------------------------
# Fidelity HAR extractor
# ---------------------------------------------------------------------------


def _extract_from_har(file_bytes: bytes) -> list[tuple[str, bytes]]:
    """Extract PDFs from a Fidelity HAR file."""
    return extract_fidelity_pdfs_from_har(file_bytes)


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
