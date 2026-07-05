"""Convert .eml files inside a ZIP archive into PDFs within a new ZIP archive."""

import email
import zipfile
from email import policy
from html import escape
from pathlib import Path
from string import Template

import click
import pdfkit

TEMPLATE_PATH = Path(__file__).parent / "zip_eml_to_pdf.template.html"


def convert_eml_zip_to_pdf(input_zip_path: str, output_zip_path: str) -> int:
    """Convert every .eml member of *input_zip_path* into a PDF in *output_zip_path*.

    Returns the number of PDFs written.
    """
    # Options to suppress command line output from wkhtmltopdf
    options = {"quiet": ""}
    html_template = TEMPLATE_PATH.read_text()

    converted = 0
    with (
        zipfile.ZipFile(input_zip_path, "r") as in_zip,
        zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as out_zip,
    ):
        # Filter for .eml files in the archive
        eml_files = [f for f in in_zip.namelist() if f.lower().endswith(".eml")]

        if not eml_files:
            click.echo("No .eml files found in the input ZIP.")
            return converted

        click.echo("Starting conversion...")
        for filename in eml_files:
            # Read the .eml file directly from the ZIP into memory
            with in_zip.open(filename, "r") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)

            # Extract the clean, high-level headers
            subject = msg.get("Subject", "No Subject")
            sender = msg.get("From", "Unknown Sender")
            recipient = msg.get("To", "Unknown Recipient")
            date = msg.get("Date", "Unknown Date")

            # Extract the email body (Targeting HTML)
            body = ""
            body_is_html = False
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    # Grab HTML if available
                    if content_type == "text/html":
                        body = part.get_content()
                        body_is_html = True
                        break
                    # Fallback to plain text if no HTML exists
                    elif content_type == "text/plain" and not body:
                        body = part.get_content()
            else:
                body = msg.get_content()
                body_is_html = msg.get_content_type() == "text/html"

            # Build a clean HTML structure combining headers and body
            html_content = Template(html_template).substitute(
                subject=escape(subject),
                sender=escape(sender),
                recipient=escape(recipient),
                date=escape(date),
                body=body if body_is_html else f"<pre>{escape(body)}</pre>",
            )

            # Convert the combined HTML into PDF bytes.
            # Passing 'False' instead of a file path forces pdfkit to return bytes
            pdf_bytes = pdfkit.from_string(html_content, False, options=options)

            # Write the generated PDF bytes directly into the output ZIP.
            # Strip the .eml extension and add .pdf
            pdf_filename = filename[:-4] + ".pdf"
            out_zip.writestr(pdf_filename, pdf_bytes)
            converted += 1

            click.echo(f"Success: {pdf_filename}")

    return converted


@click.command("zip-eml-to-pdf")
@click.argument("input_zip")
@click.argument("output_zip")
def zip_eml_to_pdf_command(input_zip: str, output_zip: str) -> None:
    """Convert EML files in a ZIP to PDFs in a new ZIP."""
    click.echo(f"Reading from: {input_zip}")
    click.echo(f"Writing to: {output_zip}")

    converted = convert_eml_zip_to_pdf(input_zip, output_zip)
    if converted:
        click.echo(
            f"\nBatch complete! Your converted files are saved in {output_zip!r}."
        )
