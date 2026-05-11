#!/usr/bin/env python3
"""Extract Fidelity PDF attachments from a HAR file.

Usage:
    python extract_fidelity_pdfs.py <har_file> <output_dir>

The script scans the HAR file for GET requests to the Fidelity
spshistoryservices endpoint, decodes the Base64-encoded PDF from
each JSON response body, and writes the resulting PDF files to the
given output directory.
"""

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path

FIDELITY_URL_PREFIX = (
    "https://netbenefitsww.fidelity.com"
    "/mybenefitsww/spshistoryservices/activities/record/c:"
)
PDF_MAGIC = b"%PDF-"


def extract_pdfs(har_path: Path, output_dir: Path) -> int:
    """Extract PDFs from *har_path* and write them to *output_dir*.

    Returns the number of PDF files written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    with har_path.open(encoding="utf-8") as fh:
        har = json.load(fh)

    entries = har.get("log", {}).get("entries", [])
    written = 0

    for entry in entries:
        request = entry.get("request", {})
        response = entry.get("response", {})

        # Only process GET requests.
        if request.get("method", "").upper() != "GET":
            continue

        url: str = request.get("url", "")
        if not url.startswith(FIDELITY_URL_PREFIX):
            continue

        # The filename is the portion after "/c:".
        pdf_name = url[len(FIDELITY_URL_PREFIX) :]
        # Strip any query string or fragment.
        pdf_name = pdf_name.split("?")[0].split("#")[0]
        # Sanitize: take only the basename to prevent path-traversal attacks
        # (e.g. a crafted URL suffix of "../../../etc/passwd" must not escape
        # the output directory).
        pdf_name = Path(pdf_name).name
        if not pdf_name:
            print(
                f"WARNING: could not determine filename from URL: {url}",
                file=sys.stderr,
            )
            continue
        pdf_filename = pdf_name + ".pdf"

        # Validate response content type.
        mime_type = response.get("content", {}).get("mimeType", "")
        if "application/json" not in mime_type:
            print(
                f"WARNING: unexpected content type '{mime_type}' for {url}; skipping",
                file=sys.stderr,
            )
            continue

        # Decode the response body text.
        content = response.get("content", {})
        body_text = content.get("text", "")
        body_encoding = content.get("encoding", "")

        if body_encoding.lower() == "base64":
            # HAR stores the body itself as Base64; decode it first.
            try:
                raw_body = base64.b64decode(body_text)
                body_str = raw_body.decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                print(
                    f"WARNING: could not decode Base64-encoded body for {url}: "
                    f"{exc}; skipping",
                    file=sys.stderr,
                )
                continue
        else:
            body_str = body_text

        if not body_str:
            print(f"WARNING: empty response body for {url}; skipping", file=sys.stderr)
            continue

        try:
            payload = json.loads(body_str)
        except json.JSONDecodeError as exc:
            print(
                f"WARNING: could not parse JSON response body for {url}: "
                f"{exc}; skipping",
                file=sys.stderr,
            )
            continue

        file_content_b64 = payload.get("fileContent")
        if file_content_b64 is None:
            print(
                f"WARNING: 'fileContent' key missing in response for {url}; skipping",
                file=sys.stderr,
            )
            continue

        try:
            pdf_bytes = base64.b64decode(file_content_b64)
        except binascii.Error as exc:
            print(
                f"WARNING: could not Base64-decode fileContent for {url}: "
                f"{exc}; skipping",
                file=sys.stderr,
            )
            continue

        if not pdf_bytes.startswith(PDF_MAGIC):
            print(
                f"WARNING: decoded data for {url} does not look like a PDF "
                f"(starts with {pdf_bytes[:8]!r}); skipping",
                file=sys.stderr,
            )
            continue

        out_path = output_dir / pdf_filename
        out_path.write_bytes(pdf_bytes)
        print(f"Wrote {out_path} ({len(pdf_bytes)} bytes)")
        written += 1

    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Fidelity PDF attachments from a HAR file."
    )
    parser.add_argument("har_file", help="Path to the HAR file")
    parser.add_argument("output_dir", help="Directory to write extracted PDF files")
    args = parser.parse_args()

    har_path = Path(args.har_file)
    if not har_path.is_file():
        print(f"ERROR: HAR file not found: {har_path}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir)

    count = extract_pdfs(har_path, output_dir)
    if count == 0:
        print(
            "No matching Fidelity PDF entries found in the HAR file.",
            file=sys.stderr,
        )
    else:
        print(f"Done. {count} PDF file(s) extracted to {output_dir}.")


if __name__ == "__main__":
    main()
