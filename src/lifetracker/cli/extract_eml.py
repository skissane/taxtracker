"""Extract attachments from a .eml file and write them into a .zip file."""

import email
import re
import zipfile
from collections.abc import Iterator
from contextlib import suppress
from email import policy
from email.generator import BytesGenerator
from email.message import EmailMessage, Message, MIMEPart
from email.utils import parsedate_to_datetime
from io import BytesIO
from typing import cast

import click


def get_attachment_payload(part: MIMEPart) -> bytes | None:
    """Return attachment bytes for regular parts and attached emails."""
    if part.get_content_type() == "message/rfc822":
        payload = part.get_payload()

        if isinstance(payload, list) and payload:
            buffer = BytesIO()
            BytesGenerator(buffer, policy=policy.default).flatten(cast(EmailMessage, payload[0]))
            return buffer.getvalue()

        if hasattr(payload, "as_bytes"):
            return cast(bytes, payload.as_bytes(policy=policy.default))

        return None

    return cast("bytes | None", part.get_payload(decode=True))


def get_email_date_prefix(part: MIMEPart) -> str:
    """Return a YYYY-MM-DD prefix for attached emails when available."""
    if part.get_content_type() != "message/rfc822":
        return ""

    payload = part.get_payload()
    if isinstance(payload, list) and payload:
        attached_message = cast(Message, payload[0])
    elif hasattr(payload, "get"):
        attached_message = cast(Message, payload)
    else:
        return ""

    date_header = attached_message.get("Date")
    if not date_header:
        return ""

    with suppress(TypeError, ValueError, IndexError, OverflowError):
        parsed_date = parsedate_to_datetime(date_header)
        return f"{parsed_date.date().isoformat()}-"

    return ""


def iter_parts_with_date_prefix(part: MIMEPart, inherited_prefix: str = "") -> Iterator[tuple[MIMEPart, str]]:
    """Depth-first walk yielding (part, date_prefix) pairs.

    Visits parts in the same depth-first order as ``Message.walk()``, but
    unlike ``walk()`` does not yield plain multipart container parts
    (``multipart/mixed``, ``multipart/alternative``, etc.) themselves - only
    leaf parts and ``message/rfc822`` wrappers are yielded. An attached
    email's date prefix is threaded down to the parts nested inside it (e.g.
    a PDF attached to a forwarded payslip email), so nested attachments
    inherit their containing email's date rather than only the ``.eml`` part
    itself.
    """
    if part.get_content_type() == "message/rfc822":
        prefix = get_email_date_prefix(part) or inherited_prefix
        yield part, prefix

        payload = part.get_payload()
        if isinstance(payload, list) and payload:
            yield from iter_parts_with_date_prefix(cast(MIMEPart, payload[0]), prefix)
        elif hasattr(payload, "get"):
            yield from iter_parts_with_date_prefix(cast(MIMEPart, payload), prefix)
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


def get_entry_name(filename: str, prefix: str, date_prefix: str) -> str:
    """Return the ZIP entry name for an attachment called *filename*.

    That's ``<date_prefix><prefix>-<filename>`` (without *prefix* and its ``-`` if it's empty), lowercased
    with each run of anything but letters and digits before the extension turned into one ``-``.
    """
    if prefix:
        filename = f"{prefix}-{filename}"
    filename = f"{date_prefix}{filename}"
    stem, dot, file_ext = filename.rpartition(".")
    if not dot or not file_ext:
        stem, file_ext = filename, ""
    stem = "-".join(re.sub(r"[^A-Za-z0-9]", " ", stem).lower().split())
    return f"{stem}.{file_ext}" if file_ext else stem


def describe_content_type(part: MIMEPart) -> str:
    """Return *part*'s content type, or for an attached email, that of the email's own body."""
    content_type = part.get_content_type()
    if content_type == "message/rfc822":
        payload = part.get_payload()
        if isinstance(payload, list) and len(payload) == 1:
            return cast(Message, payload[0]).get_content_type()
    return content_type


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
            # Get the filename of the attachment
            filename: str = part.get_filename() or ""

            # Fallback: If an attached email somehow lacks a filename, generate one
            if not filename:
                if part.get_content_type() == "message/rfc822":
                    filename = f"nested_email_{attachment_count + 1}.eml"
                else:
                    # Not an attachment, skip
                    continue

            filename = get_entry_name(filename, prefix, date_prefix)

            # 4. Extract the payload (the actual file data)
            payload = get_attachment_payload(part)
            if payload is None:
                continue

            # 5. Write the data directly into the ZIP archive
            zip_filename = get_unique_filename(filename, used_filenames)
            zipf.writestr(zip_filename, payload)
            attachment_count += 1
            click.echo(f"Added to ZIP: {zip_filename} ({describe_content_type(part)})")

    click.echo(f"\nSUCCESS: Extracted {attachment_count} attachments to {output_zip_path!r}")


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
