import datetime
from typing import override

from django.core.exceptions import ValidationError
from django.test import TestCase

from lifetracker.core.forms import FlexibleDateField


class FlexibleDateFieldTests(TestCase):
    """Tests for FlexibleDateField and the extra English date-input formats."""

    @override
    def setUp(self) -> None:
        self.field = FlexibleDateField(required=False)

    def test_iso_format_accepted(self) -> None:
        self.assertEqual(self.field.clean("2023-07-20"), datetime.date(2023, 7, 20))

    def test_day_month_year_full_name(self) -> None:
        """'20 July 2023' should parse to 2023-07-20."""
        self.assertEqual(self.field.clean("20 July 2023"), datetime.date(2023, 7, 20))

    def test_month_day_comma_year_full_name(self) -> None:
        """'June 30, 2023' should parse to 2023-06-30."""
        self.assertEqual(self.field.clean("June 30, 2023"), datetime.date(2023, 6, 30))

    def test_day_month_year_abbreviated(self) -> None:
        """'20 Jul 2023' should parse to 2023-07-20."""
        self.assertEqual(self.field.clean("20 Jul 2023"), datetime.date(2023, 7, 20))

    def test_month_day_comma_year_abbreviated(self) -> None:
        """'Jun 30, 2023' should parse to 2023-06-30."""
        self.assertEqual(self.field.clean("Jun 30, 2023"), datetime.date(2023, 6, 30))

    def test_invalid_date_string_raises(self) -> None:
        with self.assertRaises(ValidationError):
            self.field.clean("not a date")

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(self.field.clean(""))
