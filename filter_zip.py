#!/usr/bin/env python3
"""Filter a ZIP file by skipping members whose names contain a specific substring."""

import argparse
import sys
import zipfile


def filter_zip(input_path, output_path, reject_substring):
    """Reads an input zip and writes to an output zip, skipping specific files."""
    try:
        # Open the input zip for reading and the output zip for writing
        with zipfile.ZipFile(input_path, "r") as zin:
            with zipfile.ZipFile(
                output_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as zout:
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

        print(f"SUCCESS: Created {output_path!r}")
        print(f"Files kept: {kept_count} | Files skipped: {skipped_count}")

    except FileNotFoundError:
        print(f"ERROR: The input file {input_path!r} was not found.")
        sys.exit(1)
    except zipfile.BadZipFile:
        print(f"ERROR: {input_path!r} is not a valid ZIP file.")
        sys.exit(1)


def main():
    # Set up the argument parser
    parser = argparse.ArgumentParser(
        description="Filter ZIP file, skipping members whose names contain substring."
    )

    # Define the three required positional arguments
    parser.add_argument("input_zip", help="Path to the source ZIP file.")
    parser.add_argument(
        "output_zip", help="Path where the filtered ZIP file will be saved."
    )
    parser.add_argument(
        "reject_substring",
        help="Substring to check against file names. Matches will be skipped.",
    )

    # Parse arguments from the command line
    args = parser.parse_args()

    # Execute the filtering function
    filter_zip(args.input_zip, args.output_zip, args.reject_substring)


if __name__ == "__main__":
    main()
