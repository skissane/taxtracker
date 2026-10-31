"""Filter a ZIP file by skipping members whose names contain a specific substring."""

import zipfile
from pathlib import Path

import click


def filter_zip(input_path, output_path, reject_substring):
    """Reads an input zip and writes to an output zip, skipping specific files."""
    # samefile() also catches symlinks and hard links; a missing file can't be the other.
    input_file, output_file = Path(input_path), Path(output_path)
    if input_file.exists() and output_file.exists() and input_file.samefile(output_file):
        raise click.ClickException(f"Input and output ZIP paths are the same file: {input_path!r}")

    try:
        # Open the input zip for reading and the output zip for writing
        with zipfile.ZipFile(input_path, "r") as zin:
            with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                skipped_count = 0
                kept_count = 0

                # Iterate through all members in the input archive
                for item in zin.infolist():
                    if reject_substring in item.filename:
                        skipped_count += 1
                        continue  # Skip this file

                    # Read the file content from the source zip
                    content = zin.read(item.filename)
                    # Write it to the destination zip, preserving original metadata
                    zout.writestr(item, content)
                    kept_count += 1

        click.echo(f"SUCCESS: Created {output_path!r}")
        click.echo(f"Files kept: {kept_count} | Files skipped: {skipped_count}")

    except FileNotFoundError:
        raise click.ClickException(f"The input file {input_path!r} was not found.")
    except zipfile.BadZipFile:
        raise click.ClickException(f"{input_path!r} is not a valid ZIP file.")


@click.command("filter-zip")
@click.argument("input_zip")
@click.argument("output_zip")
@click.argument("reject_substring")
def filter_zip_command(input_zip: str, output_zip: str, reject_substring: str) -> None:
    """Filter ZIP file, skipping members whose names contain substring."""
    filter_zip(input_zip, output_zip, reject_substring)
