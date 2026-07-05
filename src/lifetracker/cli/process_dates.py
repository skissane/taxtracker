"""Process ISO 8601 dates, sort, deduplicate, and group by Australian Financial Year."""

import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta

import click


def get_financial_year(date_obj: date) -> int:
    """
    Returns the Australian financial year for a given date.
    The Australian FY ends on June 30.
    """
    if date_obj.month >= 7:
        return date_obj.year + 1
    return date_obj.year


def parse_date_line(clean_line: str) -> date:
    """Parse a single date line, in ISO 8601 or 'Month Dth YYYY' format."""
    try:
        return datetime.strptime(clean_line, "%Y-%m-%d").date()
    except ValueError:
        pass

    # Use regex to safely strip ordinal suffixes, e.g. 'November 20th 2024'.
    clean_line_no_suffix = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", clean_line)
    return datetime.strptime(clean_line_no_suffix, "%B %d %Y").date()


def read_dates(lines: Iterable[str], add_days: int) -> set[date]:
    """Read, parse, deduplicate, and offset dates from *lines*.

    Raises ValueError (naming the offending line) on an unparseable date.
    """
    processed_dates: set[date] = set()
    for line_num, line in enumerate(lines, 1):
        clean_line = line.strip()
        if not clean_line:
            continue

        try:
            parsed_date = parse_date_line(clean_line)
        except ValueError:
            raise ValueError(
                f"Unexpected date format on line {line_num}: {clean_line!r}"
            ) from None

        processed_dates.add(parsed_date + timedelta(days=add_days))

    return processed_dates


def group_by_financial_year(dates: set[date]) -> dict[int, list[date]]:
    """Group *dates* by Australian financial year, each group sorted chronologically."""
    fy_groups: defaultdict[int, list[date]] = defaultdict(list)
    for d in sorted(dates):
        fy_groups[get_financial_year(d)].append(d)
    return fy_groups


def render_markdown(
    fy_groups: dict[int, list[date]], summary: bool, year: int | None
) -> str:
    """Render financial-year groups as GitHub-Flavored Markdown."""
    lines: list[str] = []
    first_group = True
    for fy in sorted(fy_groups.keys()):
        if year and fy != year:
            continue  # Skip financial years that don't match the --year filter
        if not first_group:
            lines.append("")  # Blank line between tables for cleaner markdown rendering
        first_group = False

        fy_days = len(fy_groups[fy])
        lines.append(f"# FY{fy} ({fy_days} date{'s' if fy_days != 1 else ''})")
        if summary:
            continue  # Skip detailed tables if only summary is requested
        lines.append("")
        lines.append("| Date       | Day |")
        lines.append("|------------|-----|")

        for d in fy_groups[fy]:
            # %a provides the 3-letter English day abbreviation (Mon, Tue, Wed, etc.)
            day_abbr = d.strftime("%a")
            lines.append(f"| {d.isoformat()} | {day_abbr} |")

    return "\n".join(lines)


@click.command("process-dates")
@click.option(
    "--add-days",
    type=int,
    default=0,
    help="Number of days to add to each input date (default: 0)",
)
@click.option(
    "--summary",
    is_flag=True,
    help="Only show per-financial year summary",
)
@click.option(
    "--year",
    type=int,
    default=None,
    help="Only show dates from given financial year (e.g., 2024 for FY2024)",
)
def process_dates_command(add_days: int, summary: bool, year: int | None) -> None:
    """Sort/deduplicate dates, grouping by Australian Financial Year."""
    try:
        processed_dates = read_dates(sys.stdin, add_days)
    except ValueError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)

    if not processed_dates:
        return

    fy_groups = group_by_financial_year(processed_dates)
    output = render_markdown(fy_groups, summary, year)
    if output:
        click.echo(output)
