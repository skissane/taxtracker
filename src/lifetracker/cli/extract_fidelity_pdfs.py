"""Extract Fidelity PDF attachments from a HAR file.

Scans the HAR file for GET requests to the Fidelity spshistoryservices
endpoint, decodes the Base64-encoded PDF from each JSON response body, and
writes the resulting PDF files to the given output directory.
"""

import sys
from pathlib import Path

import click

from lifetracker.taxtracker.fidelity_har import UnsupportedArchiveError, extract_pdfs_from_har


def extract_pdfs(har_path: Path, output_dir: Path) -> int:
    """Extract PDFs from *har_path* and write them to *output_dir*.

    Returns the number of PDF files written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    def warn(url: str, reason: str) -> None:
        print(f"WARNING: {reason} for {url}; skipping", file=sys.stderr)

    pdfs = extract_pdfs_from_har(har_path.read_bytes(), on_skip=warn)
    written = 0
    for pdf_name, pdf_bytes in pdfs:
        out_path = output_dir / pdf_name
        out_path.write_bytes(pdf_bytes)
        print(f"Wrote {out_path} ({len(pdf_bytes)} bytes)")
        written += 1
    return written


@click.command("extract-fidelity-pdfs")
@click.argument("har_file")
@click.argument("output_dir")
def extract_fidelity_pdfs_command(har_file: str, output_dir: str) -> None:
    """Extract Fidelity PDF attachments from a HAR file."""
    har_path = Path(har_file)
    if not har_path.is_file():
        print(f"ERROR: HAR file not found: {har_path}", file=sys.stderr)
        sys.exit(1)

    try:
        count = extract_pdfs(har_path, Path(output_dir))
    except UnsupportedArchiveError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    if count == 0:
        print(
            "No matching Fidelity PDF entries found in the HAR file.",
            file=sys.stderr,
        )
    else:
        print(f"Done. {count} PDF file(s) extracted to {output_dir}.")
