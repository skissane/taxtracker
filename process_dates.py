#!/usr/bin/env python3
"""Process ISO 8601 dates, sort, deduplicate, and group by Australian Financial Year."""

import argparse
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta


def get_financial_year(date_obj: date) -> int:
    """
    Returns the Australian financial year for a given date.
    The Australian FY ends on June 30.
    """
    if date_obj.month >= 7:
        return date_obj.year + 1
    return date_obj.year


def main():
    # Set up argparse
    parser = argparse.ArgumentParser(
        description="Sort/deduplicate dates, grouping by Australian Financial Year."
    )
    parser.add_argument(
        "--add-days",
        type=int,
        default=0,
        help="Number of days to add to each input date (default: 0)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Only show per-financial year summary",
    )
    parser.add_argument(
        "--year",
        type=int,
        help="Only show dates from given financial year (e.g., 2024 for FY2024)",
    )
    args = parser.parse_args()

    processed_dates: set[date] = set()

    # Read and parse dates from stdin
    for line_num, line in enumerate(sys.stdin, 1):
        clean_line = line.strip()

        # Ignore blank lines
        if not clean_line:
            continue

        try:
            # First, attempt to validate strict ISO 8601 format (YYYY-MM-DD)
            parsed_date = datetime.strptime(clean_line, "%Y-%m-%d").date()
        except ValueError:
            try:
                # If ISO 8601 fails, try 'November 20th 2024' format.
                # Use regex to safely strip ordinal suffixes
                clean_line_no_suffix = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", clean_line)

                # Parse the cleaned string (e.g., 'November 20 2024')
                parsed_date = datetime.strptime(clean_line_no_suffix, "%B %d %Y").date()
            except ValueError:
                print(
                    f"ERROR: Unexpected date format on line {line_num}: {clean_line!r}",
                    file=sys.stderr,
                )
                sys.exit(1)

        # Apply the --add-days offset
        adjusted_date = parsed_date + timedelta(days=args.add_days)
        processed_dates.add(adjusted_date)

    if not processed_dates:
        return

    # Sort dates chronologically (deduplication is already handled by the set)
    sorted_dates: list[date] = sorted(processed_dates)

    # Group by Australian Financial Year
    fy_groups: defaultdict[int, list[date]] = defaultdict(list)
    for d in sorted_dates:
        fy = get_financial_year(d)
        fy_groups[fy].append(d)

    # Output in GitHub-Flavored Markdown
    first_group = True
    for fy in sorted(fy_groups.keys()):
        if args.year and fy != args.year:
            continue  # Skip financial years that don't match the --year filter
        if not first_group:
            print()  # Add a blank line between tables for cleaner markdown rendering
        first_group = False

        fy_days = len(fy_groups[fy])
        print(f"# FY{fy} ({fy_days} date{'s' if fy_days != 1 else ''})")
        if args.summary:
            continue  # Skip detailed tables if only summary is requested
        print()
        print("| Date       | Day |")
        print("|------------|-----|")

        for d in fy_groups[fy]:
            # %a provides the 3-letter English day abbreviation (Mon, Tue, Wed, etc.)
            day_abbr = d.strftime("%a")
            print(f"| {d.isoformat()} | {day_abbr} |")


if __name__ == "__main__":
    main()
