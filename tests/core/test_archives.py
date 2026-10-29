import io
import zipfile

import pytest

from lifetracker.core.archives import PDF_MAGIC, extract_pdfs_from_zip, select_importable_files


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_pdfs_from_zip():
    zip_bytes = _zip(
        [
            ("folder/", b""),
            ("\\", b""),
            ("folder/statement.PDF", PDF_MAGIC + b"a"),
            ("notes.txt", b"text"),
            ("fake.pdf", b"text"),
        ]
    )
    assert extract_pdfs_from_zip(zip_bytes) == ([("statement.pdf", PDF_MAGIC + b"a")], ["notes.txt", "fake.pdf"])


def test_extract_pdfs_from_zip_skips_corrupt_deflate_data():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("corrupt.pdf", PDF_MAGIC + bytes(range(256)) * 50)
        zf.writestr("good.pdf", PDF_MAGIC + b"a")
    zip_bytes = bytearray(buf.getvalue())
    data_start = zip_bytes.find(b"PK\x03\x04") + 30 + len("corrupt.pdf")
    zip_bytes[data_start : data_start + 20] = b"\xff" * 20
    assert extract_pdfs_from_zip(bytes(zip_bytes)) == ([("good.pdf", PDF_MAGIC + b"a")], ["corrupt.pdf"])


def test_extract_pdfs_from_zip_raises_bad_zip_file():
    with pytest.raises(zipfile.BadZipFile):
        extract_pdfs_from_zip(b"not a zip")


def test_select_importable_files_skips_duplicates():
    existing = {"invoice.pdf"}
    duplicate_names = []

    importable = select_importable_files(
        [("invoice.pdf", b"duplicate"), ("dir/new.pdf", b"new"), ("new.pdf", b"again")],
        existing,
        duplicate_names,
    )

    assert importable == [("new.pdf", b"new")]
    assert duplicate_names == ["invoice.pdf", "new.pdf"]
    assert existing == {"invoice.pdf", "new.pdf"}
