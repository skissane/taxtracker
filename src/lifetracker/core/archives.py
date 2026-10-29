"""Generic helpers for "Import archive" admin actions.

``extract_pdfs_from_zip(file_bytes)``
    Extract the PDFs from a ZIP archive, returning ``(extracted, skipped_names)``.
    Raises ``zipfile.BadZipFile`` if the file isn't a ZIP archive.

``select_importable_files(extracted, existing_names, duplicate_names)``
    Drop extracted files whose names are already in *existing_names*.

Module-specific formats (e.g. taxtracker's Fidelity HAR extractor) stay in
their own module, which dispatches ``.zip`` uploads here.
"""

import io
import zipfile
from pathlib import Path

__all__ = [
    "MAX_ZIP_PDF_MEMBER_BYTES",
    "MAX_ZIP_PDF_TOTAL_BYTES",
    "PDF_MAGIC",
    "extract_pdfs_from_zip",
    "select_importable_files",
]

PDF_MAGIC = b"%PDF-"
MAX_ZIP_PDF_MEMBER_BYTES = 20 * 1024 * 1024
MAX_ZIP_PDF_TOTAL_BYTES = 100 * 1024 * 1024


def extract_pdfs_from_zip(file_bytes: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Extract PDF files from a ZIP archive.

    Only entries ending with ``.pdf`` or ``.PDF`` are imported. Directory
    components are ignored and only the final path segment is used.
    Non-PDF entries, PDFs exceeding uncompressed size limits and PDFs compressed
    with anything but deflate are reported in the returned skipped list.
    Raises ``zipfile.BadZipFile`` if *file_bytes* isn't a ZIP archive.
    """
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
            # zipfile limits how much it decompresses at once only for deflate, so a bzip2 or LZMA entry could
            # still expand far beyond file_size in one call; real ZIPs of PDFs are stored or deflated anyway.
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                skipped.append(entry_name)
                continue
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


def select_importable_files(
    extracted: list[tuple[str, bytes]], existing_names: set[str], duplicate_names: list[str]
) -> list[tuple[str, bytes]]:
    """Return archive files not already present, appending duplicates to *duplicate_names*.

    Each kept file's basename is added to *existing_names*, so a name repeated
    within the same archive is only imported once.
    """
    importable = []
    for filename, file_bytes in extracted:
        base_name = Path(filename).name
        if base_name in existing_names:
            duplicate_names.append(base_name)
            continue
        importable.append((base_name, file_bytes))
        existing_names.add(base_name)
    return importable
