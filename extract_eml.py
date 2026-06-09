#!/usr/bin/env python3
"""Extract attachments from a .eml file and write them into a .zip file."""

import email
import zipfile
from argparse import ArgumentParser
from email import policy


def eml_to_zip(eml_file_path, output_zip_path):
    """
    Extracts all attachments from a .eml file and writes them directly into a .zip file.
    """
    # 1. Open and parse the outer .eml file
    with open(eml_file_path, "rb") as f:
        # Using policy.default ensures modern, standard email parsing
        msg = email.message_from_binary_file(f, policy=policy.default)

    # 2. Create the destination ZIP file
    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        attachment_count = 0

        # 3. Walk through all parts of the email
        for part in msg.walk():
            # Skip multipart containers
            if part.is_multipart():
                continue

            # Get the filename of the attachment
            filename = part.get_filename()

            # Fallback: If an attached email somehow lacks a filename, generate one
            if not filename:
                if part.get_content_type() == "message/rfc822":
                    filename = f"nested_email_{attachment_count + 1}.eml"
                else:
                    # Not an attachment, skip
                    continue

            # 4. Extract the payload (the actual file data)
            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            # 5. Write the data directly into the ZIP archive
            zipf.writestr(filename, payload)
            attachment_count += 1
            print(f"Added to ZIP: {filename}")

    print(
        f"\nSuccess! Extracted {attachment_count} attachments to '{output_zip_path}'."
    )


def main() -> None:
    parser = ArgumentParser(
        description="Extract attachments from a .eml file "
        "and write them into a .zip file."
    )
    parser.add_argument("eml_file", help="Path to the .eml file to process")
    parser.add_argument("output_zip", help="Path to the output .zip file")
    args = parser.parse_args()
    eml_to_zip(args.eml_file, args.output_zip)


# --- How to use the script ---
if __name__ == "__main__":
    main()
