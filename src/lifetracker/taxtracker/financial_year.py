from datetime import date


def financial_year_for_date(date_obj: date) -> int:
    """Return the Australian FY end-year for a date (e.g. Aug-2023 -> FY2024)."""
    return date_obj.year if date_obj.month <= 6 else date_obj.year + 1
