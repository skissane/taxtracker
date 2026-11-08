import datetime

from click.testing import CliRunner
from django.test import TestCase

from lifetracker.cli.process_dates import group_by_financial_year, process_dates_command, read_dates, render_markdown


class ProcessDatesTests(TestCase):
    def test_read_dates_parses_iso_and_ordinal_formats(self) -> None:
        dates = read_dates(["2024-01-15\n", "November 20th 2024\n", "\n", "2024-01-15\n"], 0)
        self.assertEqual(dates, {datetime.date(2024, 1, 15), datetime.date(2024, 11, 20)})

    def test_read_dates_applies_add_days(self) -> None:
        dates = read_dates(["2024-01-15\n"], 5)
        self.assertEqual(dates, {datetime.date(2024, 1, 20)})

    def test_read_dates_raises_on_bad_format(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            read_dates(["not-a-date\n"], 0)
        self.assertIn("line 1", str(ctx.exception))

    def test_group_and_render_markdown(self) -> None:
        dates = {datetime.date(2024, 6, 30), datetime.date(2024, 7, 1)}
        groups = group_by_financial_year(dates)
        self.assertEqual(set(groups.keys()), {2024, 2025})

        rendered = render_markdown(groups, summary=False, year=None)
        self.assertIn("# FY2024 (1 date)", rendered)
        self.assertIn("# FY2025 (1 date)", rendered)
        self.assertIn("| 2024-06-30 | Sun |", rendered)

    def test_render_markdown_summary_only(self) -> None:
        groups = group_by_financial_year({datetime.date(2024, 1, 1)})
        rendered = render_markdown(groups, summary=True, year=None)
        self.assertEqual(rendered, "# FY2024 (1 date)")

    def test_render_markdown_year_filter(self) -> None:
        groups = group_by_financial_year({datetime.date(2023, 1, 1), datetime.date(2024, 1, 1)})
        rendered = render_markdown(groups, summary=True, year=2024)
        self.assertEqual(rendered, "# FY2024 (1 date)")

    def test_cli_end_to_end(self) -> None:
        runner = CliRunner()
        result = runner.invoke(process_dates_command, ["--summary"], input="2024-01-01\n2024-08-01\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("FY2024", result.output)
        self.assertIn("FY2025", result.output)

    def test_cli_empty_input_produces_no_output(self) -> None:
        runner = CliRunner()
        result = runner.invoke(process_dates_command, [], input="")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "")

    def test_cli_bad_date_exits_with_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(process_dates_command, [], input="garbage\n")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("ERROR: Unexpected date format on line 1", result.output)
