#!/usr/bin/env python3
"""Extract plain text from .eml files inside a ZIP archive into a new ZIP archive."""

import argparse
import email
import zipfile
from email import policy


def main():
    # 1. Set up argparse for command-line inputs
    parser = argparse.ArgumentParser(
        description="Extract plain text from EML files in ZIP to TXT files in new ZIP"
    )
    parser.add_argument("input_zip", help="Path to the input ZIP containing .eml files")
    parser.add_argument(
        "output_zip", help="Path to the output ZIP where TXT files will be saved"
    )
    args = parser.parse_args()

    print(f"Reading from: {args.input_zip}")
    print(f"Writing to: {args.output_zip}")

    # 2. Open both ZIP files simultaneously (read one, write to the other)
    with (
        zipfile.ZipFile(args.input_zip, "r") as in_zip,
        zipfile.ZipFile(args.output_zip, "w", zipfile.ZIP_DEFLATED) as out_zip,
    ):
        # Filter for .eml files in the archive
        eml_files = [f for f in in_zip.namelist() if f.lower().endswith(".eml")]

        if not eml_files:
            print("No .eml files found in the input ZIP")
            return

        print("Starting extraction...")
        for filename in eml_files:
            # Read the .eml file directly from the ZIP into memory
            with in_zip.open(filename, "r") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)

            # 3. Extract the text/plain body
            # Safely traverses multipart/alternative and multipart/related structures
            plain_text_part = msg.get_body(preferencelist=("plain",))

            # 4. Skip the email if no text/plain body exists
            if not plain_text_part:
                print(f"Skipped: {filename} (No text/plain body found)")
                continue

            body = plain_text_part.get_content()

            # 5. Extract the headers
            subject = msg.get("Subject", "No Subject")
            sender = msg.get("From", "Unknown Sender")
            recipient = msg.get("To", "Unknown Recipient")
            date = msg.get("Date", "Unknown Date")

            # 6. Build the clean text structure combining headers and body
            text_content = (
                f"Subject: {subject}\n"
                f"From: {sender}\n"
                f"To: {recipient}\n"
                f"Date: {date}\n"
                f"{'-' * 40}\n\n"
                f"{body}"
            )

            # 7. Write the generated text directly into the output ZIP
            # Strip the .eml extension and add .txt
            txt_filename = filename[:-4] + ".txt"

            # encode the string to bytes before writing to the zip
            out_zip.writestr(txt_filename, text_content.encode("utf-8"))

            print(f"Success: {txt_filename}")

    print(f"\nBatch complete! Your extracted files are saved in {args.output_zip!r}")


if __name__ == "__main__":
    main()
