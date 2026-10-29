"""Generic helpers for archive import actions.

The ZIP-specific bits are deliberately generic so other apps can reuse them for
"Import Archive" actions without depending on taxtracker-specific models.
"""

import io
import zipfile
from pathlib import Path
from typing import Iterable, Sequence

PDF_MAGIC = b"%PDF-"

__all__ = [
    "PDF_MAGIC",
    "extract_from_zip",
    "extract_pdfs_from_zip",
    "extract_pdf_files_from_zip",
    "filter_duplicate_filenames",
    "skip_duplicate_names",
]


def extract_from_zip(file_bytes: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Extract PDF files from a ZIP archive.

    Returns ``(extracted, skipped_names)`` where each extracted entry is a
    ``(filename, content_bytes)`` tuple and skipped names are non-PDF entries or
    invalid PDFs.
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
    except zipfile.BadZipFile:
        raise


def extract_pdfs_from_zip(file_bytes: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Compatibility alias for ``extract_from_zip``."""
    return extract_from_zip(file_bytes)


def extract_pdf_files_from_zip(file_bytes: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Compatibility alias for ``extract_from_zip``."""
    return extract_from_zip(file_bytes)


def filter_duplicate_filenames(
    extracted: Sequence[tuple[str, bytes]] | Iterable[tuple[str, bytes]],
    existing_names: set[str],
) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Return ``(kept, duplicate_names)`` after skipping any filename already seen.

    ``existing_names`` is treated as a mutable set of names already imported. Names
    seen earlier in the same archive are also skipped, so the same file appearing
    twice in the same upload won't be imported twice.
    """
    kept: list[tuple[str, bytes]] = []
    duplicate_names: list[str] = []
    seen_in_batch: set[str] = set()
    duplicate_reported: set[str] = set()

    for filename, file_bytes in extracted:
        base_name = Path(filename).name
        if base_name in existing_names or base_name in seen_in_batch:
            if base_name not in duplicate_reported:
                duplicate_names.append(base_name)
                duplicate_reported.add(base_name)
            continue
        existing_names.add(base_name)
        seen_in_batch.add(base_name)
        kept.append((filename, file_bytes))

    return kept, duplicate_names


def skip_duplicate_names(
    extracted: Sequence[tuple[str, bytes]] | Iterable[tuple[str, bytes]],
    existing_names: set[str],
) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Compatibility alias for ``filter_duplicate_filenames``."""
    return filter_duplicate_filenames(extracted, existing_names)
