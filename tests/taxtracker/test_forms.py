from typing import override

from django.test import TestCase

from lifetracker.taxtracker.forms import AttachmentForm
from lifetracker.taxtracker.models import FinancialYear, Item


class AttachmentDateInputFormatTests(TestCase):
    """AttachmentForm accepts FlexibleDateField's extra English date formats."""

    @override
    def setUp(self) -> None:
        self.fy = FinancialYear.objects.create(year=2024)
        self.item = Item.objects.create(year=self.fy, title="Income", order=1)

    # ------------------------------------------------------------------
    # Integration: AttachmentForm accepts the extra formats
    # ------------------------------------------------------------------

    def _form_data(self, date_str: str) -> dict[str, object]:
        return {
            "item": self.item.pk,
            "title": "Test",
            "date": date_str,
            "notes": "",
        }

    def test_form_accepts_day_full_month_year(self) -> None:
        """'20 July 2023' must not produce a date validation error."""
        form = AttachmentForm(data=self._form_data("20 July 2023"))
        self.assertNotIn("date", form.errors)

    def test_form_accepts_full_month_day_comma_year(self) -> None:
        """'June 30, 2023' must not produce a date validation error."""
        form = AttachmentForm(data=self._form_data("June 30, 2023"))
        self.assertNotIn("date", form.errors)

    def test_form_accepts_iso_date(self) -> None:
        """ISO format '2023-07-20' must not produce a date validation error."""
        form = AttachmentForm(data=self._form_data("2023-07-20"))
        self.assertNotIn("date", form.errors)

    def test_form_rejects_invalid_date(self) -> None:
        form = AttachmentForm(data=self._form_data("not a date"))
        self.assertIn("date", form.errors)
