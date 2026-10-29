"""Archive processing helpers for the import-archive admin view.

The import flow supports both Fidelity HAR files (extracted by ``fidelity_har``) and `.zip` bundles of PDFs
(extracted by ``lifetracker.core.archives.extract_pdfs_from_zip``, shared with other modules).
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

import zipfile

from lifetracker.core.archives import extract_pdfs_from_zip
from lifetracker.taxtracker.fidelity_har import UnsupportedArchiveError, extract_pdfs_from_har

__all__ = [
    "UnsupportedArchiveError",
    "extract_from_archive",
    "extract_from_archive_with_skips",
]


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
        return extract_pdfs_from_har(file_bytes), []
    if lower.endswith(".zip"):
        try:
            return extract_pdfs_from_zip(file_bytes)
        except zipfile.BadZipFile as exc:
            raise UnsupportedArchiveError(f"Could not parse file as a ZIP archive: {exc}") from exc
    raise UnsupportedArchiveError(f"Unrecognised archive format for '{filename}'. Supported formats: .har, .zip")
