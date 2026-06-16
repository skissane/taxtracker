#!/usr/bin/env python3
"""Convert .eml files inside a ZIP archive into PDFs within a new ZIP archive."""

import argparse
import email
import zipfile
from email import policy
from html import escape
from pathlib import Path
from string import Template

import pdfkit


def main():
    # 1. Set up argparse for command-line inputs
    parser = argparse.ArgumentParser(
        description="Convert EML files in a ZIP to PDFs in a new ZIP."
    )
    parser.add_argument("input_zip", help="Path to the input ZIP containing .eml files")
    parser.add_argument(
        "output_zip", help="Path to the output ZIP where PDFs will be saved"
    )
    args = parser.parse_args()

    print(f"Reading from: {args.input_zip}")
    print(f"Writing to: {args.output_zip}")

    # Options to suppress command line output from wkhtmltopdf
    options = {"quiet": ""}

    # 2. Open both ZIP files simultaneously (read one, write to the other)
    with (
        zipfile.ZipFile(args.input_zip, "r") as in_zip,
        zipfile.ZipFile(args.output_zip, "w", zipfile.ZIP_DEFLATED) as out_zip,
    ):
        # Filter for .eml files in the archive
        eml_files = [f for f in in_zip.namelist() if f.lower().endswith(".eml")]

        if not eml_files:
            print("No .eml files found in the input ZIP.")
            return

        print("Starting conversion...")
        for filename in eml_files:
            # Read the .eml file directly from the ZIP into memory
            with in_zip.open(filename, "r") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)

            # 3. Extract the clean, high-level headers
            subject = msg.get("Subject", "No Subject")
            sender = msg.get("From", "Unknown Sender")
            recipient = msg.get("To", "Unknown Recipient")
            date = msg.get("Date", "Unknown Date")

            # 4. Extract the email body (Targeting HTML)
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

            # 5. Build a clean HTML structure combining headers and body
            html_template = (
                Path(__file__).parent / "zip_eml_to_pdf.template.html"
            ).read_text()
            html_content = Template(html_template).substitute(
                subject=escape(subject),
                sender=escape(sender),
                recipient=escape(recipient),
                date=escape(date),
                body=body if body_is_html else f"<pre>{escape(body)}</pre>",
            )

            # 6. Convert the combined HTML into PDF bytes
            # Passing 'False' instead of a file path forces pdfkit to return bytes
            pdf_bytes = pdfkit.from_string(html_content, False, options=options)

            # 7. Write the generated PDF bytes directly into the output ZIP
            # Strip the .eml extension and add .pdf
            pdf_filename = filename[:-4] + ".pdf"
            out_zip.writestr(pdf_filename, pdf_bytes)

            print(f"Success: {pdf_filename}")

    print(f"\nBatch complete! Your converted files are saved in {args.output_zip!r}.")


if __name__ == "__main__":
    main()
