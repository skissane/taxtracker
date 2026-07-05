"""Extract plain text from .eml files inside a ZIP archive into a new ZIP archive."""

import email
import zipfile
from email import policy

import click


def convert_eml_zip_to_txt(input_zip_path: str, output_zip_path: str) -> int | None:
    """Extract the plain-text body of every .eml member of *input_zip_path*
    into a .txt file in *output_zip_path*.

    Returns the number of .txt files written, or None if the input ZIP
    contained no .eml members at all.
    """
    extracted = 0
    with (
        zipfile.ZipFile(input_zip_path, "r") as in_zip,
        zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as out_zip,
    ):
        # Filter for .eml files in the archive
        eml_files = [f for f in in_zip.namelist() if f.lower().endswith(".eml")]

        if not eml_files:
            click.echo("No .eml files found in the input ZIP")
            return None

        click.echo("Starting extraction...")
        for filename in eml_files:
            # Read the .eml file directly from the ZIP into memory
            with in_zip.open(filename, "r") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)

            # Extract the text/plain body.
            # Safely traverses multipart/alternative and multipart/related structures
            plain_text_part = msg.get_body(preferencelist=("plain",))

            # Skip the email if no text/plain body exists
            if not plain_text_part:
                click.echo(f"Skipped: {filename} (No text/plain body found)")
                continue

            body = plain_text_part.get_content()

            # Extract the headers
            subject = msg.get("Subject", "No Subject")
            sender = msg.get("From", "Unknown Sender")
            recipient = msg.get("To", "Unknown Recipient")
            date = msg.get("Date", "Unknown Date")

            # Build the clean text structure combining headers and body
            text_content = (
                f"Subject: {subject}\n"
                f"From: {sender}\n"
                f"To: {recipient}\n"
                f"Date: {date}\n"
                f"{'-' * 40}\n\n"
                f"{body}"
            )

            # Write the generated text directly into the output ZIP.
            # Strip the .eml extension and add .txt
            txt_filename = filename[:-4] + ".txt"

            # encode the string to bytes before writing to the zip
            out_zip.writestr(txt_filename, text_content.encode("utf-8"))
            extracted += 1

            click.echo(f"Success: {txt_filename}")

    return extracted


@click.command("zip-eml-to-txt")
@click.argument("input_zip")
@click.argument("output_zip")
def zip_eml_to_txt_command(input_zip: str, output_zip: str) -> None:
    """Extract plain text from EML files in ZIP to TXT files in new ZIP"""
    click.echo(f"Reading from: {input_zip}")
    click.echo(f"Writing to: {output_zip}")

    extracted = convert_eml_zip_to_txt(input_zip, output_zip)
    if extracted is not None:
        click.echo(
            f"\nBatch complete! Your extracted files are saved in {output_zip!r}"
        )
