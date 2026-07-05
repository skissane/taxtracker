"""Extract attachments from a .eml file and write them into a .zip file."""

import email
import re
import zipfile
from contextlib import suppress
from email import policy
from email.generator import BytesGenerator
from email.message import EmailMessage, Message
from email.utils import parsedate_to_datetime
from io import BytesIO

import click


def get_attachment_payload(part: EmailMessage) -> bytes | None:
    """Return attachment bytes for regular parts and attached emails."""
    if part.get_content_type() == "message/rfc822":
        payload = part.get_payload()

        if isinstance(payload, list) and payload:
            buffer = BytesIO()
            BytesGenerator(buffer, policy=policy.default).flatten(payload[0])
            return buffer.getvalue()

        if hasattr(payload, "as_bytes"):
            return payload.as_bytes(policy=policy.default)

        return None

    return part.get_payload(decode=True)


def get_email_date_prefix(part: EmailMessage) -> str:
    """Return a YYYY-MM-DD prefix for attached emails when available."""
    if part.get_content_type() != "message/rfc822":
        return ""

    payload = part.get_payload()
    if isinstance(payload, list) and payload:
        attached_message = payload[0]
    elif hasattr(payload, "get"):
        attached_message = payload
    else:
        return ""

    date_header = attached_message.get("Date")
    if not date_header:
        return ""

    with suppress(TypeError, ValueError, IndexError, OverflowError):
        parsed_date = parsedate_to_datetime(date_header)
        return f"{parsed_date.date().isoformat()}-"

    return ""


def iter_parts_with_date_prefix(part: EmailMessage, inherited_prefix: str = ""):
    """Depth-first walk yielding (part, date_prefix) pairs.

    Mirrors the traversal order of ``Message.walk()``, but threads an attached
    email's date prefix down to the parts nested inside it (e.g. a PDF
    attached to a forwarded payslip email), so nested attachments inherit
    their containing email's date rather than only the ``.eml`` part itself.
    """
    if part.get_content_type() == "message/rfc822":
        prefix = get_email_date_prefix(part) or inherited_prefix
        yield part, prefix

        payload = part.get_payload()
        if isinstance(payload, list) and payload:
            yield from iter_parts_with_date_prefix(payload[0], prefix)
        elif hasattr(payload, "get"):
            yield from iter_parts_with_date_prefix(payload, prefix)
        return

    if part.is_multipart():
        for subpart in part.iter_parts():
            yield from iter_parts_with_date_prefix(subpart, inherited_prefix)
        return

    yield part, inherited_prefix


def get_unique_filename(filename: str, used_filenames: dict[str, int]) -> str:
    """Make duplicate attachment names distinct inside the ZIP."""
    if filename not in used_filenames:
        used_filenames[filename] = 1
        return filename

    stem, dot, suffix = filename.rpartition(".")
    if not dot:
        stem = filename
        suffix = ""

    index = used_filenames[filename]
    used_filenames[filename] += 1

    if suffix:
        return f"{stem}_{index}.{suffix}"

    return f"{stem}_{index}"


def eml_to_zip(eml_file_path: str, output_zip_path: str, prefix: str = "") -> None:
    """
    Extracts all attachments from a .eml file and writes them directly into a .zip file.
    """
    # 1. Open and parse the outer .eml file
    with open(eml_file_path, "rb") as f:
        # Using policy.default ensures modern, standard email parsing
        msg = email.message_from_binary_file(f, policy=policy.default)

    # 2. Create the destination ZIP file
    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        attachment_count: int = 0
        used_filenames: dict[str, int] = {}

        # 3. Walk through all parts of the email
        for part, date_prefix in iter_parts_with_date_prefix(msg):
            # Skip multipart containers, but keep attached emails.
            if part.is_multipart() and part.get_content_type() != "message/rfc822":
                continue

            # Get the filename of the attachment
            filename: str = part.get_filename() or ""

            # Fallback: If an attached email somehow lacks a filename, generate one
            if not filename:
                if part.get_content_type() == "message/rfc822":
                    filename = f"nested_email_{attachment_count + 1}.eml"
                else:
                    # Not an attachment, skip
                    continue

            if prefix:
                filename = f"{prefix}-{filename}"

            filename = f"{date_prefix}{filename}"
            file_ext = filename.rsplit(".")[-1] if "." in filename else ""
            filename = filename.removesuffix(f".{file_ext}") if file_ext else filename
            filename = "-".join(re.sub(r"[^A-Za-z0-9]", " ", filename).lower().split())
            filename = f"{filename}.{file_ext}" if file_ext else filename

            # 4. Extract the payload (the actual file data)
            payload = get_attachment_payload(part)
            if payload is None:
                continue

            # 5. Write the data directly into the ZIP archive
            zip_filename = get_unique_filename(filename, used_filenames)
            zipf.writestr(zip_filename, payload)
            attachment_count += 1
            content_type = part.get_content_type()
            if content_type == "message/rfc822":
                part_payload = part.get_payload()
                if isinstance(part_payload, list) and len(part_payload) == 1:
                    message_body: Message = part_payload[0]
                    content_type = message_body.get_content_type()
            click.echo(f"Added to ZIP: {zip_filename} ({content_type})")

    click.echo(
        f"\nSUCCESS: Extracted {attachment_count} attachments to {output_zip_path!r}"
    )


@click.command("extract-eml")
@click.argument("eml_file")
@click.argument("output_zip")
@click.option(
    "--prefix",
    type=str,
    default="",
    help="Optional prefix for all extracted filenames",
)
def extract_eml_command(eml_file: str, output_zip: str, prefix: str) -> None:
    """Extract attachments from a .eml file and write them into a .zip file."""
    eml_to_zip(eml_file, output_zip, prefix)
