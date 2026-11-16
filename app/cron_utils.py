"""Minute-based cron validation shared by configuration and scheduling."""

from datetime import datetime

from croniter import croniter


def valid_minute_schedule(expression: str) -> bool:
    if len(str(expression or "").split()) != 5 or not croniter.is_valid(expression):
        return False
    try:
        # A full Gregorian cycle also covers rare leap-day/weekday combinations.
        croniter(
            expression, datetime(2000, 1, 1), max_years_between_matches=400
        ).get_next(datetime)
    except (ValueError, OverflowError):
        return False
    return True
