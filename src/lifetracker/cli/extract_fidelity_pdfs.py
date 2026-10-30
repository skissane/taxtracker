"""Extract Fidelity PDF attachments from a HAR file.

Scans the HAR file for GET requests to the Fidelity spshistoryservices
endpoint, decodes the Base64-encoded PDF from each JSON response body, and
writes the resulting PDF files to the given output directory.
"""

from pathlib import Path

import click

from lifetracker.taxtracker.fidelity_har import UnsupportedArchiveError, extract_pdfs_from_har


def extract_pdfs(har_path: Path, output_dir: Path) -> int:
    """Extract PDFs from *har_path* and write them to *output_dir*.

    Returns the number of PDF files written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    def warn(url: str, reason: str) -> None:
        click.echo(f"WARNING: {reason} for {url}; skipping", err=True)

    pdfs: dict[str, bytes] = {}
    for pdf_name, pdf_bytes in extract_pdfs_from_har(har_path.read_bytes(), on_skip=warn):
        # A HAR can hold the same download more than once; write one copy.
        if pdfs.setdefault(pdf_name, pdf_bytes) != pdf_bytes:
            raise click.ClickException(f"The HAR file has different PDFs named {pdf_name}")
    # Check every name before writing anything, so a refusal leaves no partial output.
    for pdf_name in pdfs:
        out_path = output_dir / pdf_name
        if out_path.exists():
            raise click.ClickException(f"Refusing to overwrite existing file: {out_path}")
    for pdf_name, pdf_bytes in pdfs.items():
        out_path = output_dir / pdf_name
        out_path.write_bytes(pdf_bytes)
        click.echo(f"Wrote {out_path} ({len(pdf_bytes)} bytes)")
    return len(pdfs)


@click.command("extract-fidelity-pdfs")
@click.argument("har_file")
@click.argument("output_dir")
def extract_fidelity_pdfs_command(har_file: str, output_dir: str) -> None:
    """Extract Fidelity PDF attachments from a HAR file."""
    har_path = Path(har_file)
    if not har_path.is_file():
        raise click.ClickException(f"HAR file not found: {har_path}")

    try:
        count = extract_pdfs(har_path, Path(output_dir))
    except UnsupportedArchiveError as exc:
        raise click.ClickException(str(exc))
    if count == 0:
        raise click.ClickException("No matching Fidelity PDF entries found in the HAR file.")
    click.echo(f"Done. {count} PDF file(s) extracted to {output_dir}.")
